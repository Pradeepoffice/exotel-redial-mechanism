from flask import Flask, request, jsonify
import requests
import time
import logging

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
EXOTEL_CALLER_ID    = "918044319050"          # UPDATE THIS
YOUR_SERVER_URL     = "https://your-render-app.onrender.com"  # UPDATE AFTER DEPLOY

# ─── Genesys Configuration ────────────────────────────────────────────────────
GENESYS_NUMBER      = "sip:trmum1ba17debad89d12e25f1a4e"

# ─── Redial Configuration ─────────────────────────────────────────────────────
MAX_RETRIES         = 3
DROP_DURATION_LIMIT = 10    # seconds - calls dropped within 0-10 sec = redial
RETRY_WAIT_SECONDS  = 4     # wait before redialing


# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status"  : "Exotel Redial Service is Running",
        "account" : EXOTEL_ACCOUNT_SID,
        "genesys" : GENESYS_NUMBER
    })


# ─── Call Status Webhook ──────────────────────────────────────────────────────
@app.route('/call-status', methods=['POST'])
def call_status():
    try:
        data = request.form

        # ── Extract call details sent by Exotel ───────────────────────────────
        dial_call_duration = int(data.get('DialCallDuration', 0))
        dial_call_status   = data.get('DialCallStatus', '').lower()
        call_sid           = data.get('CallSid', '')
        caller_number      = data.get('From', '')
        retry_count        = int(request.args.get('retry_count', 0))

        # ── Log all incoming details ──────────────────────────────────────────
        logger.info("=" * 60)
        logger.info(f"CallSid           : {call_sid}")
        logger.info(f"Caller Number     : {caller_number}")
        logger.info(f"Dial Call Duration: {dial_call_duration} sec")
        logger.info(f"Dial Call Status  : {dial_call_status}")
        logger.info(f"Retry Count       : {retry_count}")
        logger.info("=" * 60)

        # ── Drop Detection Logic (0 to 10 seconds) ────────────────────────────
        is_dropped = (
            dial_call_duration <= DROP_DURATION_LIMIT and
            dial_call_status in ['completed', 'no-answer', 'failed', 'busy']
        )

        # ── Redial if dropped and retries remaining ───────────────────────────
        if is_dropped and retry_count < MAX_RETRIES:
            retry_count += 1

            logger.warning(f"⚠️  Call dropped within {dial_call_duration}s!")
            logger.warning(f"🔄  Triggering Redial Attempt #{retry_count} of {MAX_RETRIES}")

            # Wait before redialing
            time.sleep(RETRY_WAIT_SECONDS)

            # Trigger redial
            success = trigger_redial(caller_number, retry_count)

            if success:
                logger.info(f"✅  Redial #{retry_count} triggered successfully")
                return jsonify({
                    "status"         : "redial_triggered",
                    "attempt"        : retry_count,
                    "call_sid"       : call_sid,
                    "drop_duration"  : dial_call_duration
                }), 200
            else:
                logger.error(f"❌  Redial #{retry_count} failed")
                return jsonify({
                    "status"  : "redial_failed",
                    "attempt" : retry_count
                }), 500

        # ── Max retries reached ───────────────────────────────────────────────
        elif is_dropped and retry_count >= MAX_RETRIES:
            logger.error(f"🚫  Max retries ({MAX_RETRIES}) reached for {caller_number}. Giving up.")
            return jsonify({
                "status"          : "max_retries_reached",
                "caller_number"   : caller_number,
                "total_attempts"  : retry_count,
                "last_duration"   : dial_call_duration
            }), 200

        # ── Normal call completed ─────────────────────────────────────────────
        else:
            logger.info(f"✅  Call completed normally. Duration: {dial_call_duration}s")
            return jsonify({
                "status"    : "call_completed_normally",
                "duration"  : dial_call_duration,
                "call_sid"  : call_sid
            }), 200

    except Exception as e:
        logger.error(f"❌  Error in call_status: {str(e)}")
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
            'StatusCallback'      : f"{YOUR_SERVER_URL}/call-status?retry_count={retry_count}",
            'StatusCallbackEvent' : 'terminal',
            'CustomField'         : f"redial_attempt_{retry_count}"
        }

        logger.info(f"📞  Calling Exotel API for redial...")
        logger.info(f"    From    : {caller_number}")
        logger.info(f"    To      : {GENESYS_NUMBER}")
        logger.info(f"    Attempt : {retry_count}")

        response = requests.post(url, data=payload)

        logger.info(f"    API Response Code : {response.status_code}")
        logger.info(f"    API Response Body : {response.text}")

        return response.status_code in [200, 201]

    except Exception as e:
        logger.error(f"❌  Error in trigger_redial: {str(e)}")
        return False


# ─── Call Logs Endpoint (optional - to view recent activity) ──────────────────
@app.route('/logs', methods=['GET'])
def get_logs():
    return jsonify({
        "message"           : "Check your Render dashboard logs for full details",
        "config": {
            "account_sid"       : EXOTEL_ACCOUNT_SID,
            "genesys_number"    : GENESYS_NUMBER,
            "max_retries"       : MAX_RETRIES,
            "drop_limit_sec"    : DROP_DURATION_LIMIT,
            "retry_wait_sec"    : RETRY_WAIT_SECONDS
        }
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
