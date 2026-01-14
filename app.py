from flask import Flask, request, render_template, send_from_directory, abort, jsonify
from flask_socketio import SocketIO, emit
import os
import re
import time
import json
import atexit
from threading import Lock
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
    "secret": {
        "vix": None,
        "gvz": None,
        "buy": None,
        "sell": None,
        "vold": None,
        "war": None,
    },
    "_server_ts": None
}

STATE_LOCK = Lock()

# ---- State persistence (warm start cache) ----
STATE_FILE = os.environ.get("STATE_FILE", "/tmp/marketmonitor_state.json")
STATE_MAX_AGE_SECS = 60 * 60 * 24 * 45  # 45 days

def _load_state_from_disk() -> None:
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            return
        ts = cached.get("_server_ts")
        if isinstance(ts, (int, float)) and time.time() - ts > STATE_MAX_AGE_SECS:
            return
        with STATE_LOCK:
            for k in ("cycle", "vol", "flow", "count", "sahm", "_server_ts"):
                if k in cached:
                    STATE[k] = cached.get(k)
            if isinstance(cached.get("secret"), dict):
                for sk in STATE["secret"]:
                    if sk in cached["secret"]:
                        STATE["secret"][sk] = cached["secret"].get(sk)
    except Exception as e:
        print("State load error:", e)

def _save_state_to_disk() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("State save error:", e)

atexit.register(_save_state_to_disk)
_load_state_from_disk()

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
    return (
        (req.headers.get("X-Webhook-Secret") or "").strip() == WEBHOOK_SECRET or
        (req.args.get("secret") or "").strip() == WEBHOOK_SECRET
    )

def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))

def _merge_field_payload(data: Dict[str, Any]) -> None:
    if data.get("cycle") is not None:
        STATE["cycle"] = str(data["cycle"]).upper()
    if data.get("vol") is not None:
        STATE["vol"] = str(data["vol"]).upper()
    if data.get("flow") is not None:
        STATE["flow"] = str(data["flow"])
    if data.get("count") is not None:
        try:
            STATE["count"] = _clamp_int(int(float(data["count"])), 0, 100)
        except Exception:
            pass
    if data.get("sahm") is not None:
        try:
            STATE["sahm"] = float(data["sahm"])
        except Exception:
            pass

def _parse_card_payload(data: Dict[str, Any]) -> None:
    try:
        card_n = int(data.get("card"))
    except Exception:
        return

    msg = str(data.get("msg") or "").strip()

    if card_n == 1 and msg:
        parts = msg.split()
        STATE["cycle"] = parts[0].upper()
        STATE["vol"] = parts[-1].upper()

    elif card_n == 2 and msg:
        STATE["flow"] = msg

    elif card_n == 3:
        m = re.search(r"(\d+(\.\d+)?)\s*%", msg)
        if m:
            STATE["count"] = _clamp_int(int(float(m.group(1))), 0, 100)
        if data.get("regime"):
            STATE["cycle"] = str(data["regime"]).upper()

    elif card_n == 4:
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.I)
        if m:
            STATE["sahm"] = float(m.group(1))

    elif card_n in (5, 6, 7, 8, 9):
        pack = {
            "name": str(data.get("name") or "").upper(),
            "symbol": str(data.get("symbol") or ""),
            "state": str(data.get("state") or ""),
            "level": int(float(data["level"])) if data.get("level") not in (None, "na") else None,
            "value": float(data["value"]) if data.get("value") not in (None, "na") else None,
        }

        if card_n == 5:
            STATE["secret"]["vix"] = pack
        elif card_n == 6:
            STATE["secret"]["gvz"] = pack
        elif card_n == 7:
            STATE["secret"]["buy"] = pack
        elif card_n == 8:
            STATE["secret"]["sell"] = pack
        elif card_n == 9:
            STATE["secret"]["vold"] = {"level": pack["level"], "state": pack["state"]}

        vixL = (STATE["secret"]["vix"] or {}).get("level")
        gvzL = (STATE["secret"]["gvz"] or {}).get("level")

        war = False
        reasons = []
        if isinstance(vixL, int) and vixL <= 4:
            war = True
            reasons.append(f"Institutional X: LEVEL {vixL}")
        if isinstance(gvzL, int) and (gvzL <= 3 or gvzL >= 8):
            war = True
            reasons.append(f"Institutional Y: LEVEL {gvzL}")

        STATE["secret"]["war"] = {"active": war, "reason": ", ".join(reasons)}

@app.route("/webhook", methods=["POST"])
def webhook():
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            abort(400)

        with STATE_LOCK:
            STATE["_server_ts"] = time.time()
            if "card" in data:
                _parse_card_payload(data)
            _merge_field_payload(data)
            _save_state_to_disk()

        socketio.emit("macro_update", STATE)
        return "SUCCESS", 200

    except Exception as e:
        print("Webhook error:", e)
        return str(e), 400

@app.route("/verify_secret", methods=["POST"])
def verify_secret():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown").split(",")[0]
    now = time.time()
    ATTEMPTS[ip] = [t for t in ATTEMPTS.get(ip, []) if now - t < ATTEMPT_WINDOW_SECS]

    if len(ATTEMPTS[ip]) >= ATTEMPT_MAX:
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    if (request.get_json(silent=True) or {}).get("password") == VAULT_PASSWORD:
        return jsonify({"ok": True}), 200

    ATTEMPTS[ip].append(now)
    return jsonify({"ok": False}), 401

@socketio.on("connect")
def on_connect():
    with STATE_LOCK:
        emit("macro_update", STATE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
