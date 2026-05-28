from flask import Flask, request, jsonify
import requests
import time
import logging
import traceback

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── Exotel Configuration ─────────────────────────────────────────────────────
EXOTEL_ACCOUNT_SID  = "meesho10m"
EXOTEL_API_KEY      = "b31874cadcc6bd508645586f004f91b8f584796b6a0e2cf2"
EXOTEL_API_TOKEN    = "d3c47f486c82e184ead7f2f20b07c348d6aafa4882cf07fa"
EXOTEL_SUBDOMAIN    = "api.in.exotel.com"
EXOTEL_CALLER_ID    = "08044620216"     # Caller ID shown to customer
EXOTEL_DID_NUMBER   = "08044620216"     # DID mapped to Genesys flow — UPDATE if different
YOUR_SERVER_URL     = "https://exotel-redial-mechanism.onrender.com"

# ─── Genesys Configuration ────────────────────────────────────────────────────
GENESYS_NUMBER      = "sip:trmum17668bd8e0426a4eaee1a18"

# ─── Redial Configuration ─────────────────────────────────────────────────────
MAX_RETRIES         = 3
DROP_DURATION_LIMIT = 10
RETRY_WAIT_SECONDS  = 4


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status"        : "Exotel Redial Service is Running",
        "account"       : EXOTEL_ACCOUNT_SID,
        "did_number"    : EXOTEL_DID_NUMBER,
        "genesys"       : GENESYS_NUMBER,
        "max_retries"   : MAX_RETRIES,
        "drop_limit"    : f"{DROP_DURATION_LIMIT} sec"
    })


# ─── Call Status Webhook ──────────────────────────────────────────────────────
@app.route('/call-status', methods=['GET', 'POST'])
def call_status():
    try:
        # ── Parse raw query string ────────────────────────────────────────────
        from urllib.parse import unquote_plus
        raw_qs = request.query_string.decode('utf-8')
        logger.info(f"RAW QS : {raw_qs}")

        params = {}
        if raw_qs:
            for pair in raw_qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    k = unquote_plus(k)
                    v = unquote_plus(v)
                    params[k] = v

        # ── Extract call details ──────────────────────────────────────────────
        call_sid            = params.get('CallSid', '')
        caller_number       = params.get('From', '')
        call_to             = params.get('To', '')
        dial_call_status    = params.get('DialCallStatus', '').lower().strip()
        dial_call_duration  = int(params.get('DialCallDuration', 0))
        call_type           = params.get('CallType', '')
        retry_count         = int(params.get('retry_count', 0))

        # ── Extract Leg details ───────────────────────────────────────────────
        leg_number          = params.get('Legs[0][Number]', '')
        leg_duration_raw    = params.get('Legs[0][OnCallDuration]', '0')
        leg_cause           = params.get('Legs[0][Cause]', '')
        leg_cause_code      = params.get('Legs[0][CauseCode]', '')
        disconnected_by     = params.get('Legs[0][DisconnectedBy]', '')

        # ── Safe int conversion ───────────────────────────────────────────────
        try:
            leg_duration = int(leg_duration_raw)
        except (ValueError, TypeError):
            logger.warning(f"Could not parse leg_duration: '{leg_duration_raw}'")
            leg_duration = 0

        # ── Log all details ───────────────────────────────────────────────────
        logger.info("=" * 60)
        logger.info(f"CallSid            : {call_sid}")
        logger.info(f"Caller Number      : {caller_number}")
        logger.info(f"Called Number      : {call_to}")
        logger.info(f"Dial Call Duration : {dial_call_duration} sec")
        logger.info(f"Leg Duration       : {leg_duration} sec")
        logger.info(f"Dial Call Status   : {dial_call_status}")
        logger.info(f"Call Type          : {call_type}")
        logger.info(f"Leg Number         : {leg_number}")
        logger.info(f"Cause Code         : {leg_cause_code}")
        logger.info(f"Cause              : {leg_cause}")
        logger.info(f"Disconnected By    : {disconnected_by}")
        logger.info(f"Retry Count        : {retry_count}")
        logger.info("=" * 60)

        # ── Drop detection (0 to 10 sec) ──────────────────────────────────────
        duration_check  = leg_duration <= DROP_DURATION_LIMIT
        status_check    = dial_call_status in [
                            'completed', 'no-answer', 'failed', 'busy'
                          ]
        is_dropped      = duration_check and status_check

        logger.info(f"Duration Check : {leg_duration}s <= {DROP_DURATION_LIMIT}s = {duration_check}")
        logger.info(f"Status Check   : '{dial_call_status}' matched = {status_check}")
        logger.info(f"Is Dropped     : {is_dropped}")
        logger.info(f"Retries Left   : {MAX_RETRIES - retry_count}")

        # ── Redial ────────────────────────────────────────────────────────────
        if is_dropped and retry_count < MAX_RETRIES:
            retry_count += 1
            logger.warning(f"⚠️  DROP DETECTED!")
            logger.warning(f"    Caller          : {caller_number}")
            logger.warning(f"    Leg Duration    : {leg_duration}s")
            logger.warning(f"    Disconnected By : {disconnected_by}")
            logger.warning(f"    Cause Code      : {leg_cause_code}")
            logger.warning(f"🔄  Redialing via DID {EXOTEL_DID_NUMBER} — Attempt #{retry_count} of {MAX_RETRIES}")

            time.sleep(RETRY_WAIT_SECONDS)

            success = trigger_redial(caller_number, retry_count)

            if success:
                logger.info(f"✅  Redial #{retry_count} triggered successfully")
                return jsonify({
                    "status"        : "redial_triggered",
                    "attempt"       : retry_count,
                    "call_sid"      : call_sid,
                    "caller"        : caller_number,
                    "dialed_did"    : EXOTEL_DID_NUMBER,
                    "leg_duration"  : leg_duration,
                    "disconnected"  : disconnected_by
                }), 200
            else:
                logger.error(f"❌  Redial #{retry_count} API call failed")
                return jsonify({
                    "status"  : "redial_failed",
                    "attempt" : retry_count
                }), 500

        # ── Max retries reached ───────────────────────────────────────────────
        elif is_dropped and retry_count >= MAX_RETRIES:
            logger.error(f"🚫 Max retries ({MAX_RETRIES}) reached for {caller_number}")
            return jsonify({
                "status"         : "max_retries_reached",
                "caller_number"  : caller_number,
                "total_attempts" : retry_count,
                "last_duration"  : leg_duration
            }), 200

        # ── Normal call ───────────────────────────────────────────────────────
        else:
            logger.info(f"✅ Normal call. Duration={leg_duration}s")
            return jsonify({
                "status"          : "call_completed_normally",
                "leg_duration"    : leg_duration,
                "dial_duration"   : dial_call_duration,
                "call_sid"        : call_sid,
                "disconnected_by" : disconnected_by
            }), 200

    except Exception as e:
        logger.error(f"❌ EXCEPTION:")
        logger.error(traceback.format_exc())
        return jsonify({
            "error" : str(e),
            "trace" : traceback.format_exc()
        }), 500


# ─── Redial via Exotel DID ────────────────────────────────────────────────────
def trigger_redial(caller_number, retry_count):
    try:
        url = (
            f"https://{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}"
            f"@{EXOTEL_SUBDOMAIN}/v1/Accounts/"
            f"{EXOTEL_ACCOUNT_SID}/Calls/connect"
        )

        # ── From = caller's number ─────────────────────────────────────────
        # ── To   = Exotel DID mapped to Genesys flow ──────────────────────
        # Exotel will ring the caller, and when answered,
        # connect them through the DID flow to Genesys
        payload = {
            'From'                : caller_number,      # 09790571549
            'To'                  : EXOTEL_DID_NUMBER,  # 08044620216 → flow → Genesys
            'CallerId'            : EXOTEL_CALLER_ID,   # Shown to caller
            'TimeLimit'           : 3600,
            'StatusCallback'      : (
                f"{YOUR_SERVER_URL}/call-status"
                f"?retry_count={retry_count}"
            ),
            'StatusCallbackEvent' : 'terminal',
            'CustomField'         : f"redial_attempt_{retry_count}"
        }

        logger.info(f"📞  Redial via Exotel DID...")
        logger.info(f"    Caller (From)   : {caller_number}")
        logger.info(f"    DID (To)        : {EXOTEL_DID_NUMBER}")
        logger.info(f"    CallerID        : {EXOTEL_CALLER_ID}")
        logger.info(f"    Attempt         : #{retry_count}")
        logger.info(f"    StatusCallback  : {payload['StatusCallback']}")

        response = requests.post(url, data=payload, timeout=10)

        logger.info(f"    API Status  : {response.status_code}")
        logger.info(f"    API Body    : {response.text}")

        return response.status_code in [200, 201]

    except Exception as e:
        logger.error(f"❌  trigger_redial error:")
        logger.error(traceback.format_exc())
        return False


# ─── Debug Endpoint ───────────────────────────────────────────────────────────
@app.route('/debug', methods=['GET', 'POST'])
def debug():
    try:
        from urllib.parse import unquote_plus
        raw_qs = request.query_string.decode('utf-8')
        params = {}
        if raw_qs:
            for pair in raw_qs.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    k = unquote_plus(k)
                    v = unquote_plus(v)
                    params[k] = v
        return jsonify({
            "raw_query_string"  : raw_qs,
            "parsed_params"     : params,
            "leg_duration"      : params.get('Legs[0][OnCallDuration]', 'NOT FOUND'),
            "disconnected_by"   : params.get('Legs[0][DisconnectedBy]', 'NOT FOUND'),
            "cause_code"        : params.get('Legs[0][CauseCode]', 'NOT FOUND'),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Config Check ─────────────────────────────────────────────────────────────
@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "account_sid"    : EXOTEL_ACCOUNT_SID,
        "did_number"     : EXOTEL_DID_NUMBER,
        "caller_id"      : EXOTEL_CALLER_ID,
        "genesys_number" : GENESYS_NUMBER,
        "max_retries"    : MAX_RETRIES,
        "drop_limit_sec" : DROP_DURATION_LIMIT,
        "retry_wait_sec" : RETRY_WAIT_SECONDS,
        "server_url"     : YOUR_SERVER_URL
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
