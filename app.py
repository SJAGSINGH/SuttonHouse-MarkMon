from flask import Flask, request, render_template, send_from_directory, abort, jsonify
from flask_socketio import SocketIO, emit
import os
import re
import time
import json
import atexit
import copy
from threading import Lock
from typing import Any, Dict, Optional

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
    "monitor": None,  # optional if you add later
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
# Render disks typically mount to /var/data. If present, prefer it automatically.
# You can still override with STATE_FILE env.
DEFAULT_STATE_FILE = "/var/data/marketmonitor_state.json" if os.path.isdir("/var/data") else "/tmp/marketmonitor_state.json"
STATE_FILE = os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)
STATE_MAX_AGE_SECS = 60 * 60 * 24 * 45  # 45 days


# ----------------------------
# Helpers
# ----------------------------
def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))

def _safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def _safe_int(v) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except Exception:
        return None

def _normalise_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


def _authorised_webhook(req) -> bool:
    if not WEBHOOK_SECRET:
        return True
    return (
        (req.headers.get("X-Webhook-Secret") or "").strip() == WEBHOOK_SECRET or
        (req.args.get("secret") or "").strip() == WEBHOOK_SECRET
    )


def _load_state_from_disk() -> None:
    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)

        if not isinstance(cached, dict):
            return

        ts = cached.get("_server_ts")

        if isinstance(ts, (int, float)):
            age_secs = time.time() - (ts / 1000.0)  # ts is ms
            if age_secs > STATE_MAX_AGE_SECS:
                return

        with STATE_LOCK:
            for k in ("cycle", "vol", "flow", "count", "sahm", "monitor", "_server_ts"):
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
        # ensure directory exists (important for /var/data)
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)

        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("State save error:", e)


def _recompute_war_from_secret() -> None:
    """Ensure war.active always reflects current vix/gvz levels."""
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


def _get_payload_any() -> Dict[str, Any]:
    """
    TradingView can send:
      - JSON
      - form-encoded (request.form)
      - or JSON string in a single form field
    This function returns a dict or raises ValueError.
    """
    # 1) try JSON
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # 2) try form fields
    if request.form and len(request.form) > 0:
        d = dict(request.form)
        # sometimes Pine sends {"message":"{...json...}"} or a single key holding JSON
        if len(d) == 1:
            only_val = list(d.values())[0]
            if isinstance(only_val, str) and only_val.strip().startswith("{"):
                try:
                    inner = json.loads(only_val)
                    if isinstance(inner, dict):
                        return inner
                except Exception:
                    pass
        return d

    # 3) try raw body as JSON
    raw = (request.data or b"").decode("utf-8", errors="ignore").strip()
    if raw.startswith("{") and raw.endswith("}"):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("No valid payload found (expected JSON or form fields)")


# ----------------------------
# Merge logic (field-based payload)
# ----------------------------
def _merge_field_payload(data: Dict[str, Any]) -> None:
    """
    Accepts both your canonical keys and common alternative key names.
    Only overwrites fields when new value is not None/empty.
    """
    # Canonical keys
    cycle = _normalise_str(data.get("cycle"))
    vol   = _normalise_str(data.get("vol"))
    flow  = _normalise_str(data.get("flow"))
    count = data.get("count")
    sahm  = data.get("sahm")

    # Common alternates (if your Pine changed names)
    cycle_alt = _normalise_str(data.get("regime")) or _normalise_str(data.get("cycle_regime"))
    vol_alt   = _normalise_str(data.get("volatility")) or _normalise_str(data.get("vix_state"))
    flow_alt  = _normalise_str(data.get("rotation")) or _normalise_str(data.get("capital_rotation"))
    count_alt = data.get("maturity") or data.get("cycle_maturity")
    sahm_alt  = data.get("sahm_value") or data.get("sahm_trigger")

    if cycle is None and cycle_alt is not None:
        cycle = cycle_alt
    if vol is None and vol_alt is not None:
        vol = vol_alt
    if flow is None and flow_alt is not None:
        flow = flow_alt
    if count is None and count_alt is not None:
        count = count_alt
    if sahm is None and sahm_alt is not None:
        sahm = sahm_alt

    if cycle is not None:
        STATE["cycle"] = str(cycle).upper()

    if vol is not None:
        STATE["vol"] = str(vol).upper()

    if flow is not None:
        STATE["flow"] = str(flow)

    if count is not None:
        c = _safe_int(count)
        if c is not None:
            STATE["count"] = _clamp_int(c, 0, 100)

    if sahm is not None:
        s = _safe_float(sahm)
        if s is not None:
            STATE["sahm"] = s


# ----------------------------
# Card-based payload parsing
# ----------------------------
def _parse_card_payload(data: Dict[str, Any]) -> None:
    """
    Keeps your existing card mapping but makes card-1 parsing more tolerant.
    """
    card_n = _safe_int(data.get("card"))
    if card_n is None:
        return

    msg = _normalise_str(data.get("msg")) or ""

    if card_n == 1 and msg:
        # Try explicit patterns first
        # Example: "CYCLE: COMMODITIES | VOL: ELEVATED"
        m_cycle = re.search(r"(?:CYCLE|REGIME)\s*:\s*([A-Za-z ]+)", msg, re.I)
        m_vol   = re.search(r"(?:VOL|VOLATILITY)\s*:\s*([A-Za-z ]+)", msg, re.I)

        if m_cycle:
            STATE["cycle"] = m_cycle.group(1).strip().upper()
        if m_vol:
            STATE["vol"] = m_vol.group(1).strip().upper()

        # Fallback: old behaviour (first token / last token) if we still didn't get anything
        if not STATE.get("cycle") or not STATE.get("vol"):
            parts = msg.split()
            if len(parts) >= 2:
                STATE["cycle"] = (STATE.get("cycle") or parts[0]).upper()
                STATE["vol"]   = (STATE.get("vol") or parts[-1]).upper()

    elif card_n == 2 and msg:
        STATE["flow"] = msg

    elif card_n == 3:
        # Pull percentage from msg like "MATURITY 45%"
        m = re.search(r"(\d+(\.\d+)?)\s*%", msg)
        if m:
            STATE["count"] = _clamp_int(int(float(m.group(1))), 0, 100)

        # Some scripts send regime on card 3
        reg = _normalise_str(data.get("regime")) or _normalise_str(data.get("cycle"))
        if reg:
            STATE["cycle"] = reg.upper()

    elif card_n == 4:
        # SAHM: 0.52
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.I)
        if m:
            STATE["sahm"] = float(m.group(1))

    elif card_n in (5, 6, 7, 8, 9):
        # Secret pack
        level = None
        if data.get("level") not in (None, "na", "NA", ""):
            level = _safe_int(data.get("level"))

        value = None
        if data.get("value") not in (None, "na", "NA", ""):
            value = _safe_float(data.get("value"))

        pack = {
            "name": (_normalise_str(data.get("name")) or "").upper(),
            "symbol": _normalise_str(data.get("symbol")) or "",
            "state": _normalise_str(data.get("state")) or "",
            "level": level,
            "value": value,
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

        _recompute_war_from_secret()


# ----------------------------
# Flask routes
# ----------------------------
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
    # include persistence diagnostics so you can instantly confirm if it's saving to /var/data or /tmp
    with STATE_LOCK:
        snap = copy.deepcopy(STATE)
    return jsonify({
        "ok": True,
        "state": snap,
        "state_file": STATE_FILE,
        "state_file_exists": os.path.exists(STATE_FILE),
    }), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = _get_payload_any()
        if not isinstance(data, dict):
            abort(400)

        with STATE_LOCK:
            now_ms = int(time.time() * 1000)
            STATE["_server_ts"] = now_ms

            # Card-based parsing (optional)
            if "card" in data:
                _parse_card_payload(data)

            # Field-based merge (optional)
            _merge_field_payload(data)

            # Ensure war recompute if vix/gvz already exist but war missing
            if STATE["secret"].get("war") is None:
                _recompute_war_from_secret()

            _save_state_to_disk()
            payload = copy.deepcopy(STATE)

        socketio.emit("macro_update", payload)
        return "SUCCESS", 200

    except Exception as e:
        print("Webhook error:", e)
        return str(e), 400

# ============================================================
# INGEST MACRO (Python feeder endpoint)
# ============================================================
# This is the new canonical ingest route for your Python macro engine.
# Use it when you want to POST state updates from Python → server → UI.
#
# Call example:
#   POST /ingest_macro?secret=WEBHOOK_SECRET   (or header X-Webhook-Secret)
#   JSON body: {"cycle":"...", "vol":"...", "flow":"...", "count":45, "sahm":0.33, "secret": {...}}
#
# IMPORTANT:
# - This reuses the same merge + emit logic you already trust.
# - It is tolerant to payload shapes: {state:{...}}, {payload:{...}}, {data:{...}}.
# - It stamps _server_ts in ms for your RSC validator.
# ============================================================

@app.route("/ingest_macro", methods=["POST"])
def ingest_macro():
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = _get_payload_any()
        if not isinstance(data, dict):
            abort(400)

        # unwrap common envelopes
        if "state" in data and isinstance(data["state"], dict):
            data = data["state"]
        elif "payload" in data and isinstance(data["payload"], dict):
            data = data["payload"]
        elif "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        with STATE_LOCK:
            now_ms = int(time.time() * 1000)
            STATE["_server_ts"] = now_ms

            # If your python feeder ever sends card packets, keep this.
            # Otherwise harmless.
            if "card" in data:
                _parse_card_payload(data)

            # Canonical merge for cycle/vol/flow/count/sahm (+ alternates)
            _merge_field_payload(data)

            # Optional: allow python feeder to send full secret block
            # (safe merge — only overwrite when dict)
            if isinstance(data.get("secret"), dict):
                for sk in STATE["secret"].keys():
                    if sk in data["secret"]:
                        STATE["secret"][sk] = data["secret"][sk]

            # Ensure war is always consistent with vix/gvz if missing
            if STATE["secret"].get("war") is None:
                _recompute_war_from_secret()
            else:
                # also keep it in sync if vix/gvz updated
                _recompute_war_from_secret()

            _save_state_to_disk()
            payload = copy.deepcopy(STATE)

        socketio.emit("macro_update", payload)
        return jsonify({"ok": True}), 200

    except Exception as e:
        print("Ingest macro error:", e)
        return str(e), 400


# ============================================================
# WEBHOOK (optional legacy TradingView ingest)
# ============================================================
# Keep this ONLY if you still want TradingView → server direct.
# If your plan is Python-only, you can remove this route or leave it in place.
# If leaving it: simplest is to forward internally to ingest_macro logic.
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    # If you still want TradingView direct → UI, keep this as-is.
    # If you want to deprecate it, return 410 or redirect logic.
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = _get_payload_any()
        if not isinstance(data, dict):
            abort(400)

        with STATE_LOCK:
            now_ms = int(time.time() * 1000)
            STATE["_server_ts"] = now_ms

            if "card" in data:
                _parse_card_payload(data)

            _merge_field_payload(data)

            if STATE["secret"].get("war") is None:
                _recompute_war_from_secret()
            else:
                _recompute_war_from_secret()

            _save_state_to_disk()
            payload = copy.deepcopy(STATE)

        socketio.emit("macro_update", payload)
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
        # keep _server_ts as ms so the front-end validator behaves
        if not isinstance(STATE.get("_server_ts"), (int, float)):
            STATE["_server_ts"] = int(time.time() * 1000)
        emit("macro_update", copy.deepcopy(STATE))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)

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
        if not isinstance(STATE.get("_server_ts"), (int, float)):
            STATE["_server_ts"] = int(time.time() * 1000)
        emit("macro_update", copy.deepcopy(STATE))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
