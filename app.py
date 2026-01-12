
from flask import Flask, request, render_template, send_from_directory, abort, jsonify
from flask_socketio import SocketIO, emit
import os
import re
import time
from typing import Any, Dict

app = Flask(__name__, static_folder="static")

# Threading mode = compatible with Gunicorn gthreads on Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Optional: set WEBHOOK_SECRET in Render env vars to lock the endpoint.
WEBHOOK_SECRET = (os.environ.get("WEBHOOK_SECRET") or "").strip()

# This is the ONLY schema your index.html needs:
# { cycle, vol, flow, count, sahm }
STATE: Dict[str, Any] = {
    "cycle": None,      # "COMMODITIES" / "EQUITIES"
    "vol": None,        # "ELEVATED" / "STABLE"
    "flow": None,       # e.g. "INTO COMMODITIES"
    "count": None,      # int 0..100
    "sahm": None,       # float
    "_server_ts": None  # server timestamp for debugging/health
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


def _authorised(req) -> bool:
    """
    Supports either:
      - Header: X-Webhook-Secret: <secret>
      - Query:  /webhook?secret=<secret>
    If WEBHOOK_SECRET is empty, auth is disabled (dev mode).
    """
    if not WEBHOOK_SECRET:
        return True
    header_secret = (req.headers.get("X-Webhook-Secret") or "").strip()
    query_secret = (req.args.get("secret") or "").strip()
    return header_secret == WEBHOOK_SECRET or query_secret == WEBHOOK_SECRET


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def _merge_field_payload(data: Dict[str, Any]) -> None:
    """
    If you ever switch Pine to field-based JSON later, this will still work.
    """
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
    """
    Converts your Pine card payloads into STATE fields:
      card 1: regime + vol
      card 2: flow
      card 3: count + regime
      card 4: sahm
    """
    card = data.get("card")
    msg = str(data.get("msg") or "").strip()

    try:
        card_n = int(card)
    except Exception:
        return

    if card_n == 1:
        # "COMMODITIES ABOVE SMA (RISING) ELEVATED"
        # first token is regime, last token is vol
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

        # your Pine includes "regime": "COMMODITIES"
        if data.get("regime"):
            STATE["cycle"] = str(data["regime"]).upper()

    elif card_n == 4:
        # "SAHM:0.63"
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.IGNORECASE)
        if m:
            STATE["sahm"] = float(m.group(1))


@app.route("/webhook", methods=["POST"])
def webhook():
    if not _authorised(request):
        print("Rejected webhook: unauthorised")
        abort(401)

    try:
        data = request.get_json(force=True, silent=False)
        if not isinstance(data, dict):
            abort(400)

        # Timestamp
        STATE["_server_ts"] = time.time()

        # Log
        print(f"Incoming Webhook: {data}")

        # Normalise both possible schemas
        if "card" in data and "msg" in data:
            _parse_card_payload(data)

        _merge_field_payload(data)

        # Emit the unified STATE (the UI’s expected schema)
        socketio.emit("macro_update", STATE)

        return "SUCCESS", 200

    except Exception as e:
        print(f"Error in webhook: {e}")
        return str(e), 400


@socketio.on("connect")
def on_connect():
    # New tab / refresh instantly gets current state
    emit("macro_update", STATE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
