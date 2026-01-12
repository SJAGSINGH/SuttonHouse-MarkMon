from flask import Flask, request, render_template, send_from_directory, abort, jsonify
from flask_socketio import SocketIO, emit
import os
import re
import time
from typing import Any, Dict

app = Flask(__name__, static_folder="static")

# Threading mode = compatible with Gunicorn gthreads on Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Optional: lock TradingView webhook (set in Render env)
WEBHOOK_SECRET = (os.environ.get("WEBHOOK_SECRET") or "").strip()

# Server-side vault password (set in Render env)
VAULT_PASSWORD = (os.environ.get("VAULT_PASSWORD") or "toffees").strip()

# ---- Simple brute-force limiter (per IP) ----
# Allow 6 attempts per 5 minutes
ATTEMPTS: Dict[str, list] = {}
ATTEMPT_WINDOW_SECS = 5 * 60
ATTEMPT_MAX = 6

# Unified state expected by your index.html
STATE: Dict[str, Any] = {
    "cycle": None,
    "vol": None,
    "flow": None,
    "count": None,
    "sahm": None,
    "_server_ts": None
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "state": STATE}), 200


def _authorised_webhook(req) -> bool:
    if not WEBHOOK_SECRET:
        return True
    header_secret = (req.headers.get("X-Webhook-Secret") or "").strip()
    query_secret = (req.args.get("secret") or "").strip()
    return header_secret == WEBHOOK_SECRET or query_secret == WEBHOOK_SECRET


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def _merge_field_payload(data: Dict[str, Any]) -> None:
    if "cycle" in data and data["cycle"] is not None:
        STATE["cycle"] = str(data["cycle"]).upper()
    if "vol" in data and data["vol"] is not None:
        STATE["vol"] = str(data["vol"]).upper()
    if "flow" in data and data["flow"] is not None:
        STATE["flow"] = str(data["flow"])
    if "count" in data and data["count"] is not None:
        try:
            STATE["count"] = _clamp_int(int(float(data["count"])), 0, 100)
        except Exception:
            pass
    if "sahm" in data and data["sahm"] is not None:
        try:
            STATE["sahm"] = float(data["sahm"])
        except Exception:
            pass


def _parse_card_payload(data: Dict[str, Any]) -> None:
    card = data.get("card")
    msg = str(data.get("msg") or "").strip()

    try:
        card_n = int(card)
    except Exception:
        return

    if card_n == 1:
        # "COMMODITIES ABOVE SMA (RISING) ELEVATED"
        parts = msg.split()
        if parts:
            STATE["cycle"] = parts[0].upper()
            STATE["vol"] = parts[-1].upper()

    elif card_n == 2:
        # "INTO COMMODITIES"
        if msg:
            STATE["flow"] = msg

    elif card_n == 3:
        # msg like "87%"
        m = re.search(r"(\d+(\.\d+)?)\s*%", msg)
        if m:
            STATE["count"] = _clamp_int(int(float(m.group(1))), 0, 100)
        if data.get("regime"):
            STATE["cycle"] = str(data["regime"]).upper()

    elif card_n == 4:
        # "SAHM:0.63"
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.IGNORECASE)
        if m:
            STATE["sahm"] = float(m.group(1))


@app.route("/webhook", methods=["POST"])
def webhook():
    if not _authorised_webhook(request):
        print("Rejected webhook: unauthorised")
        abort(401)

    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            abort(400)

        STATE["_server_ts"] = time.time()
        print(f"Incoming Webhook: {data}")

        # Normalise TradingView/Pine formats
        if "card" in data and "msg" in data:
            _parse_card_payload(data)
        _merge_field_payload(data)

        socketio.emit("macro_update", STATE)
        return "SUCCESS", 200

    except Exception as e:
        print(f"Error in webhook: {e}")
        return str(e), 400


@app.route("/verify_secret", methods=["POST"])
def verify_secret():
    """
    Frontend posts {"password":"..."}.
    Returns 200 {ok:true} if correct, 401 if incorrect, 429 if rate-limited.
    """
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()

    # Clean old attempts
    history = ATTEMPTS.get(ip, [])
    history = [t for t in history if now - t < ATTEMPT_WINDOW_SECS]
    ATTEMPTS[ip] = history

    if len(history) >= ATTEMPT_MAX:
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    data = request.get_json(force=True, silent=True) or {}
    pw = str(data.get("password") or "").strip()

    if pw == VAULT_PASSWORD:
        return jsonify({"ok": True}), 200

    # record failed attempt
    ATTEMPTS[ip].append(now)
    return jsonify({"ok": False}), 401


@socketio.on("connect")
def on_connect():
    # New tab / refresh instantly gets current macro state
    emit("macro_update", STATE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
