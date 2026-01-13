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
ATTEMPTS: Dict[str, list] = {}
ATTEMPT_WINDOW_SECS = 5 * 60
ATTEMPT_MAX = 6

# Unified state expected by index.html (+ secret block)
STATE: Dict[str, Any] = {
    "cycle": None,
    "vol": None,
    "flow": None,
    "count": None,
    "sahm": None,

    # ✅ secret indicators (cards 5–9)
    "secret": {
        "vix": None,    # dict: {value, level, state, symbol, name}
        "gvz": None,    # dict: {value, level, state, symbol, name}
        "buy": None,    # dict: {value, level, state, symbol, name}
        "sell": None,   # dict: {value, level, state, symbol, name}
        "vold": None,   # dict: {level, state}
        "war": None,    # dict: {active, reason}
    },

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
    # Backwards compatible: if Pine ever sends cycle/vol/flow/count/sahm directly
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

    try:
        card_n = int(card)
    except Exception:
        return

    # msg is optional for secret cards
    msg = str(data.get("msg") or "").strip()

    if card_n == 1:
        parts = msg.split()
        if parts:
            STATE["cycle"] = parts[0].upper()
            STATE["vol"] = parts[-1].upper()

    elif card_n == 2:
        if msg:
            STATE["flow"] = msg  # can be "ROTATION ACTIVE | ..."

    elif card_n == 3:
        m = re.search(r"(\d+(\.\d+)?)\s*%", msg)
        if m:
            STATE["count"] = _clamp_int(int(float(m.group(1))), 0, 100)
        if data.get("regime"):
            STATE["cycle"] = str(data["regime"]).upper()

    elif card_n == 4:
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.IGNORECASE)
        if m:
            STATE["sahm"] = float(m.group(1))

    # ✅ SECRET CARDS 5–9 (structured JSON from Pine; no msg required)
    elif card_n in (5, 6, 7, 8, 9):
        name = str(data.get("name") or "").strip().upper()
        symbol = str(data.get("symbol") or "").strip()
        state = str(data.get("state") or "").strip()

        level_raw = data.get("level")
        value_raw = data.get("value")

        level = None
        try:
            if level_raw is not None:
                level = int(float(level_raw))
        except Exception:
            level = None

        value = None
        try:
            if value_raw is not None and str(value_raw).lower() != "na":
                value = float(value_raw)
        except Exception:
            value = None

        pack = {"name": name, "symbol": symbol, "state": state, "level": level, "value": value}

        if card_n == 5:
            STATE["secret"]["vix"] = pack
        elif card_n == 6:
            STATE["secret"]["gvz"] = pack
        elif card_n == 7:
            STATE["secret"]["buy"] = pack
        elif card_n == 8:
            STATE["secret"]["sell"] = pack
        elif card_n == 9:
            STATE["secret"]["vold"] = {"level": level, "state": state}

        # ✅ WAR ROOM logic (server-side)
        vix = STATE["secret"].get("vix") or {}
        gvz = STATE["secret"].get("gvz") or {}
        vixL = vix.get("level")
        gvzL = gvz.get("level")

        # VIX: 1–4 = interest (1 extreme)
        # GVZ: 1–3 = negative extreme interest (1 extreme) OR 8–10 = high interest
        war = False
        reason = []

        if isinstance(vixL, int) and vixL <= 4:
            war = True
            reason.append(f"Institutional X: LEVEL {vixL}")

        if isinstance(gvzL, int) and (gvzL <= 3 or gvzL >= 8):
            war = True
            reason.append(f"Institutional Y: LEVEL {gvzL}")


        STATE["secret"]["war"] = {"active": war, "reason": ", ".join(reason) if reason else ""}


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

        # ✅ FIX: parse whenever card exists (msg optional for secret cards)
        if "card" in data:
            _parse_card_payload(data)

        _merge_field_payload(data)

        socketio.emit("macro_update", STATE)
        return "SUCCESS", 200

    except Exception as e:
        print(f"Error in webhook: {e}")
        return str(e), 400


@app.route("/verify_secret", methods=["POST"])
def verify_secret():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()

    history = ATTEMPTS.get(ip, [])
    history = [t for t in history if now - t < ATTEMPT_WINDOW_SECS]
    ATTEMPTS[ip] = history

    if len(history) >= ATTEMPT_MAX:
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    data = request.get_json(force=True, silent=True) or {}
    pw = str(data.get("password") or "").strip()

    if pw == VAULT_PASSWORD:
        return jsonify({"ok": True}), 200

    ATTEMPTS[ip].append(now)
    return jsonify({"ok": False}), 401


@socketio.on("connect")
def on_connect():
    emit("macro_update", STATE)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
