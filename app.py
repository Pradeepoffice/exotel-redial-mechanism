from flask import Flask, request, jsonify
import requests
import time
import logging
from urllib.parse import parse_qs, unquote

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
EXOTEL_CALLER_ID    = "08044620216"
YOUR_SERVER_URL     = "https://exotel-redial-mechanism.onrender.com"  # UPDATE THIS

# ─── Genesys Configuration ────────────────────────────────────────────────────
GENESYS_NUMBER      = "sip:trmum17668bd8e0426a4eaee1a18"

# ─── Redial Configuration ─────────────────────────────────────────────────────
MAX_RETRIES         = 3
DROP_DURATION_LIMIT = 10    # seconds
RETRY_WAIT_SECONDS  = 4     # wait before redialing


# ─── Helper: Parse Raw Query String ───────────────────────────────────────────
def parse_raw_params(req):
    """
    Parses raw query string to handle Legs[0][OnCallDuration]
    type parameters that Flask cannot parse directly.
    """
    try:
        # Get raw query string and decode it
        raw = req.query_string.decode('utf-8')
        logger.info(f"Raw Query String: {raw}")

        # Parse into dict (handles URL encoded brackets)
        parsed = parse_qs(raw, keep_blank_values=True)

        # Flatten single-value lists
        flat = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

        logger.info(f"Parsed Params: {flat}")
        return flat

    except Exception as e:
        logger.error(f"Error parsing raw params: {str(e)}")
        return {}


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status"  : "Exotel Redial Service is Running",
        "account" : EXOTEL_ACCOUNT_SID,
        "genesys" : GENESYS_NUMBER
    })


# ─── Call Status Webhook ──────────────────────────────────────────────────────
@app.route('/call-status', methods=['GET', 'POST'])
def call_status():
    try:
        # ── Parse all parameters safely ───────────────────────────────────────
        params = parse_raw_params(request)

        # ── Extract standard call details ─────────────────────────────────────
        dial_call_duration  = int(params.get('DialCallDuration', 0))
        dial_call_status    = params.get('DialCallStatus', '').lower()
        call_sid            = params.get('CallSid', '')
        caller_number       = params.get('From', '')
        call_to             = params.get('To', '')
        call_type           = params.get('CallType', '')
        retry_count         = int(params.get('retry_count', 0))

        # ── Extract Leg Details (URL decoded brackets) ────────────────────────
        leg_number      = params.get('Legs[0][Number]', '')
        leg_duration    = int(params.get('Legs[0][OnCallDuration]', 0))
        leg_cause       = params.get('Legs[0][Cause]', '')
        leg_cause_code  = params.get('Legs[0][CauseCode]', '')
        disconnected_by = params.get('Legs[0][DisconnectedBy]', '')

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

        # ── Drop Detection Logic (0 to 10 seconds) ────────────────────────────
        is_dropped = (
            leg_duration <= DROP_DURATION_LIMIT and
            dial_call_status in ['completed', 'no-answer', 'failed', 'busy']
        )

        logger.info(f"Is Dropped? : {is_dropped}")
        logger.info(f"Leg Duration ({leg_duration}s) <= Limit ({DROP_DURATION_LIMIT}s) : {leg_duration <= DROP_DURATION_LIMIT}")
        logger.info(f"Status Match: {dial_call_status in ['completed', 'no-answer', 'failed', 'busy']}")

        # ── Redial if dropped and retries remaining ───────────────────────────
        if is_dropped and retry_count < MAX_RETRIES:
            retry_count += 1

            logger.warning(f"⚠️  Call dropped! Leg Duration : {leg_duration}s")
            logger.warning(f"    Disconnected By : {disconnected_by}")
            logger.warning(f"    Cause Code      : {leg_cause_code}")
            logger.warning(f"🔄  Redial Attempt  : #{retry_count} of {MAX_RETRIES}")

            # Wait before redialing
            time.sleep(RETRY_WAIT_SECONDS)

            # Trigger redial
            success = trigger_redial(caller_number, retry_count)

            if success:
                logger.info(f"✅  Redial #{retry_count} triggered successfully")
                return jsonify({
                    "status"        : "redial_triggered",
                    "attempt"       : retry_count,
                    "call_sid"      : call_sid,
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
            logger.error(
                f"🚫 Max retries ({MAX_RETRIES}) reached for {caller_number}"
            )
            return jsonify({
                "status"         : "max_retries_reached",
                "caller_number"  : caller_number,
                "total_attempts" : retry_count,
                "last_duration"  : leg_duration
            }), 200

        # ── Normal call completed ─────────────────────────────────────────────
        else:
            logger.info(
                f"✅ Normal call. "
                f"Leg Duration: {leg_duration}s | "
                f"Disconnected By: {disconnected_by}"
            )
            return jsonify({
                "status"          : "call_completed_normally",
                "leg_duration"    : leg_duration,
                "dial_duration"   : dial_call_duration,
                "call_sid"        : call_sid,
                "disconnected_by" : disconnected_by
            }), 200

    except Exception as e:
        logger.error(f"❌ Error in call_status: {str(e)}")
        # Log full traceback for debugging
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


# ─── Redial Function ──────────────────────────────────────────────────────────
def trigger_redial(caller_number, retry_count):
    try:
        url = (
            f"https://{EXOTEL_API_KEY}:{EXOTEL_API_TOKEN}"
            f"@{EXOTEL_SUBDOMAIN}/v1/Accounts/"
            f"{EXOTEL_ACCOUNT_SID}/Calls/connect"
        )

        payload = {
            'From'                : caller_number,
            'To'                  : GENESYS_NUMBER,
            'CallerId'            : EXOTEL_CALLER_ID,
            'TimeLimit'           : 3600,
            'StatusCallback'      : (
                f"{YOUR_SERVER_URL}/call-status"
                f"?retry_count={retry_count}"
            ),
            'StatusCallbackEvent' : 'terminal',
            'CustomField'         : f"redial_attempt_{retry_count}"
        }

        logger.info(f"📞  Calling Exotel Redial API...")
        logger.info(f"    From     : {caller_number}")
        logger.info(f"    To       : {GENESYS_NUMBER}")
        logger.info(f"    Attempt  : {retry_count}")
        logger.info(f"    Callback : {payload['StatusCallback']}")

        response = requests.post(url, data=payload)

        logger.info(f"    API Status : {response.status_code}")
        logger.info(f"    API Body   : {response.text}")

        return response.status_code in [200, 201]

    except Exception as e:
        logger.error(f"❌  Error in trigger_redial: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False


# ─── Config Check ─────────────────────────────────────────────────────────────
@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "account_sid"    : EXOTEL_ACCOUNT_SID,
        "genesys_number" : GENESYS_NUMBER,
        "caller_id"      : EXOTEL_CALLER_ID,
        "max_retries"    : MAX_RETRIES,
        "drop_limit_sec" : DROP_DURATION_LIMIT,
        "retry_wait_sec" : RETRY_WAIT_SECONDS,
        "server_url"     : YOUR_SERVER_URL
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
