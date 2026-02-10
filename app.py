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
from collections import deque
from datetime import datetime

app = Flask(__name__, static_folder="static")

# Threading mode = compatible with Gunicorn gthreads on Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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

    # ✅ Card 2 — canonical, nested ONLY
    "card2": {
        "state": None,
        "text": None,
        "time": None,
        "tf": None,
        "ref_id": None,
    },

    "monitor": {
        "last_by_ref": {},
        "last_by_ticker": {},
        "last_hello": {},
    },
    "nodes": {
        "by_ref": {},   # "12": { "last_setup": {...}, "last_scada_status": {...}, "last_watch": {...}, "_ts": {...} }
    },

    "secret": {
        "vix": None,
        "gvz": None,
        "buy": None,
        "sell": None,
        "vold": None,
        "war": None,
    },

    "_server_ts": None,
}

STATE_LOCK = Lock()
from collections import deque
from datetime import datetime

DEBUG_MAX = 250
DEBUG_LOG = deque(maxlen=DEBUG_MAX)
DEBUG_LOCK = Lock()

def _iso(ts_ms):
    if not ts_ms:
        return ""
    return datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")

def _safe_short_json(obj, limit=2000):
    try:
        s = json.dumps(obj, ensure_ascii=False)
        return s if len(s) <= limit else s[:limit] + "…"
    except Exception:
        return str(obj)[:limit]

def _extract_meta(data):
    return {
        "type": str(data.get("type") or "NA"),
        "ref_id": int(data["ref_id"]) if str(data.get("ref_id", "")).isdigit() else None,
        "ticker": data.get("ticker"),
        "tf": data.get("tf"),
        "time": data.get("time"),
    }

def _log_debug(path, data, ok=True, err=None):
    entry = {
        "ts": int(time.time() * 1000),
        "path": path,
        "ok": ok,
        "err": err,
        "meta": _extract_meta(data) if isinstance(data, dict) else {},
        "raw": _safe_short_json(data),
    }
    with DEBUG_LOCK:
        DEBUG_LOG.appendleft(entry)

def _update_monitor_lane(meta):
    now = int(time.time() * 1000)

    ref = meta.get("ref_id")
    ticker = meta.get("ticker")
    typ = meta.get("type")

    if ref is not None:
        STATE["monitor"]["last_by_ref"][str(ref)] = {
            "ts": now,
            "type": typ,
            "ticker": ticker,
        }

    if ticker:
        STATE["monitor"]["last_by_ticker"][ticker] = {
            "ts": now,
            "type": typ,
            "ref_id": ref,
        }

    if typ.startswith("HELLO"):
        rec = STATE["monitor"]["last_hello"].get(ticker, {})
        if "OPEN" in typ:
            rec["open"] = now
        elif "CLOSE" in typ:
            rec["close"] = now
        else:
            rec["test"] = now
        rec["ref_id"] = ref
        STATE["monitor"]["last_hello"][ticker] = rec
# ---- State persistence (warm start cache) ----
DEFAULT_STATE_FILE = "/var/data/marketmonitor_state.json" if os.path.isdir("/var/data") else "/tmp/marketmonitor_state.json"
STATE_FILE = os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)
STATE_MAX_AGE_SECS = 60 * 60 * 24 * 45  # 45 days


# ----------------------------
# Helpers
# ----------------------------

NODE_TYPES = {"SETUP", "SCADA_STATUS", "WATCH"}

def _store_node_payload(data: Dict[str, Any]) -> None:
    """
    Stores the latest payload for SETUP / SCADA_STATUS / WATCH keyed by ref_id.
    Keeps last payload per type and a timestamp per type.
    """
    try:
        typ = str(data.get("type") or "").strip().upper()
        if typ not in NODE_TYPES:
            return

        ref = data.get("ref_id")
        if ref is None:
            return
        # TradingView sometimes sends numbers as strings
        try:
            ref_i = int(float(ref))
        except Exception:
            return

        now = int(time.time() * 1000)
        ref_key = str(ref_i)

        if "nodes" not in STATE or not isinstance(STATE.get("nodes"), dict):
            STATE["nodes"] = {"by_ref": {}}
        if "by_ref" not in STATE["nodes"] or not isinstance(STATE["nodes"].get("by_ref"), dict):
            STATE["nodes"]["by_ref"] = {}

        rec = STATE["nodes"]["by_ref"].get(ref_key)
        if not isinstance(rec, dict):
            rec = {"_ts": {}}

        k = f"last_{typ.lower()}"
        rec[k] = data
        rec["_ts"][k] = now

        # also store common convenience fields
        if data.get("ticker"):
            rec["ticker"] = str(data.get("ticker")).upper()
        rec["ref_id"] = ref_i

        STATE["nodes"]["by_ref"][ref_key] = rec

    except Exception:
        # fail-soft: never break webhook
        return

def _handle_stock_payload(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    typ = str(msg.get("type") or "").strip().upper()
    if typ not in ("SCADA_STATUS", "WATCH"):
        return None

    ref_id = msg.get("ref_id")
    if ref_id is None:
        return None

    # normalize a few fields
    try:
        msg["ref_id"] = int(float(ref_id))
    except Exception:
        return None

    if "ticker" in msg and msg["ticker"] is not None:
        msg["ticker"] = str(msg["ticker"]).upper()

    # timestamp passthrough
    if "_server_ts" not in msg:
        msg["_server_ts"] = int(time.time() * 1000)

    # store
    if typ == "SCADA_STATUS":
        STATE["stocks"]["last_scada_by_ref"][str(msg["ref_id"])] = msg
    else:
        STATE["stocks"]["last_watch_by_ref"][str(msg["ref_id"])] = msg

    return msg

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
    return s if s else None

# Optional: lock webhook/ingest endpoints (set in Render env)
WEBHOOK_SECRET = (os.environ.get("WEBHOOK_SECRET") or "").strip()
def _authorised_webhook(req) -> bool:
    """
    Validates webhook secret via header or query param.
    If no secret configured, allow all (dev-safe).
    """
    if not WEBHOOK_SECRET:
        return True
    return (
        (req.headers.get("X-Webhook-Secret") or "").strip() == WEBHOOK_SECRET
        or (req.args.get("secret") or "").strip() == WEBHOOK_SECRET
    )


def _get_payload_any() -> Dict[str, Any]:
    """
    Accepts:
      • JSON body
      • form-encoded payload
      • JSON string inside a single form field
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    if request.form:
        d = dict(request.form)
        if len(d) == 1:
            only_val = next(iter(d.values()))
            if isinstance(only_val, str) and only_val.strip().startswith("{"):
                try:
                    parsed = json.loads(only_val)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
        return d

    raw = (request.data or b"").decode("utf-8", errors="ignore").strip()
    if raw.startswith("{") and raw.endswith("}"):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("No valid payload found (expected JSON or form fields)")


def _normalise_server_ts(ts) -> Optional[int]:
    """
    Accepts seconds or milliseconds.
    Always returns milliseconds or None.
    """
    try:
        ts = int(ts)
        if ts < 1_000_000_000_000:  # seconds → ms
            ts *= 1000
        return ts
    except Exception:
        return None


def _load_state_from_disk() -> None:
    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)

        if not isinstance(cached, dict):
            return

        ts = _normalise_server_ts(cached.get("_server_ts"))
        if ts:
            age_secs = time.time() - (ts / 1000)
            if age_secs > STATE_MAX_AGE_SECS:
                return

        with STATE_LOCK:
            for k in ("cycle", "vol", "flow", "count", "sahm", "monitor", "_server_ts"):
                if k in cached:
                    STATE[k] = cached.get(k)

            if isinstance(cached.get("secret"), dict):
                for sk in STATE["secret"]:
                    STATE["secret"][sk] = cached["secret"].get(sk)

    except Exception as e:
        print("State load error:", e)


def _save_state_to_disk() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"

        def _safe(obj):
            try:
                json.dumps(obj)
                return obj
            except Exception:
                return str(obj)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_safe(STATE), f, ensure_ascii=False)

        os.replace(tmp, STATE_FILE)

    except Exception as e:
        print("State save error:", repr(e))



def _recompute_war_from_secret() -> None:
    """
    Founder-only haze trigger (formerly 'war').
    Driven solely by institutional extremes.
    """
    vixL = (STATE["secret"].get("vix") or {}).get("level")
    gvzL = (STATE["secret"].get("gvz") or {}).get("level")

    active = False
    reasons = []

    if isinstance(vixL, int) and vixL <= 4:
        active = True
        reasons.append(f"Institutional X: {vixL}")

    if isinstance(gvzL, int) and (gvzL <= 3 or gvzL >= 8):
        active = True
        reasons.append(f"Institutional Y: {gvzL}")

    STATE["secret"]["war"] = {
        "active": active,
        "reason": ", ".join(reasons)
    }


# ----------------------------
# Merge logic (field-based payload)
# ----------------------------
def _merge_field_payload(data: Dict[str, Any]) -> None:
    cycle = _normalise_str(data.get("cycle"))
    vol   = _normalise_str(data.get("vol"))
    flow  = _normalise_str(data.get("flow"))
    count = data.get("count")
    sahm  = data.get("sahm")

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
# Typed payload parsing (type-based)
# Supports: {"type":"CARD2", ...} etc.
# ----------------------------
def _parse_typed_payload(data: Dict[str, Any]) -> None:
    """
    Accepts typed payloads such as:
      {"type":"CARD2","state":"GREEN","text":"...","_server_ts":...}
      {"type":"CARD1","cycle":"...","vol":"..."}
      {"type":"CARD3","count":55,"cycle":"..."}
      {"type":"CARD4","sahm":0.42,"spx_dd":12.3}

    Fail-soft: ignore unknown / incomplete.
    """

    t = _normalise_str(data.get("type"))
    if not t:
        return

    typ = t.strip().upper()

    # helper to pull values with fallbacks
    def pick(*keys):
        for k in keys:
            if k in data and data.get(k) not in (None, "", "NA", "na"):
                return data.get(k)
        return None

    # -----------------------------------------
    # CARD 1 (Regime + Vol)
    # -----------------------------------------
    if typ in ("CARD1", "MACRO_CARD1", "REGIME_VOL"):
        cycle = pick("cycle", "regime", "cycle_regime")
        vol   = pick("vol", "volatility", "vix_state")

        if cycle is not None:
            STATE["cycle"] = str(cycle).strip().upper()
        if vol is not None:
            STATE["vol"] = str(vol).strip().upper()
        return

        # -----------------------------------------
    # CARD 2 (Capital Rotation / Short-term bias lane)
    # We store:
    #   STATE["flow"]         -> legacy Card2 renderer
    #   STATE["card2_state"]  -> explicit state lane
    #   STATE["card2_text"]   -> explicit text lane
    #   STATE["card2"]        -> nested object lane (UI-friendly)
    # -----------------------------------------
    if typ in ("CARD2", "MACRO_CARD2", "CAPITAL_ROTATION"):
        st = pick("card2_state", "state", "bias", "signal", "colour", "color")
        tx = pick("card2_text", "text", "msg", "message", "flow")

        # normalise state
        st_norm = None
        if st is not None:
            st_norm = str(st).strip().upper()
            STATE["card2_state"] = st_norm

        # normalise text
        tx_norm = None
        if tx is not None:
            tx_norm = str(tx).strip()
            STATE["card2_text"] = tx_norm

            # keep backwards compatibility (your public Card2 uses flow)
            if tx_norm:
                STATE["flow"] = tx_norm

        # ✅ ALSO populate the nested dict so UI can read data.card2.state/text
        if "card2" not in STATE or not isinstance(STATE.get("card2"), dict):
            STATE["card2"] = {"state": None, "text": None, "time": None, "tf": None, "ref_id": None}

        if st_norm is not None:
            STATE["card2"]["state"] = st_norm
        if tx_norm is not None:
            STATE["card2"]["text"] = tx_norm

        # optional metadata passthrough (safe)
        for k in ("time", "tf", "ref_id"):
            if k in data and data.get(k) not in (None, "", "NA", "na"):
                STATE["card2"][k] = data.get(k)

        return


    # -----------------------------------------
    # CARD 3 (Cycle clock)
    # -----------------------------------------
    if typ in ("CARD3", "MACRO_CARD3", "CYCLE_CLOCK"):
        count = pick("count", "maturity", "cycle_maturity")
        cycle = pick("cycle", "regime", "cycle_regime")

        if count is not None:
            c = _safe_int(count)
            if c is not None:
                STATE["count"] = _clamp_int(c, 0, 100)

        if cycle is not None:
            STATE["cycle"] = str(cycle).strip().upper()

        return

    # -----------------------------------------
    # CARD 4 (Recession pulse)
    # NOTE: in /webhook you *still* Pine-lock this.
    # This parser is mainly for /ingest_macro or future typed feeds.
    # -----------------------------------------
    if typ in ("CARD4", "MACRO_CARD4", "RECESSION_PULSE"):
        sahm = pick("sahm", "sahm_value", "sahm_trigger")
        dd   = pick("spx_dd", "spxDrawdown", "dd", "drawdown")

        if sahm is not None:
            s = _safe_float(sahm)
            if s is not None:
                STATE["sahm"] = s

        if dd is not None:
            try:
                STATE["spx_dd"] = float(dd)
            except Exception:
                pass

        return

    # Unknown typed payload: ignore silently
    return

# ----------------------------
# Card-based payload parsing
# ----------------------------
def _parse_card_payload(data: Dict[str, Any]) -> None:
    card_n = _safe_int(data.get("card"))
    if card_n is None:
        return

    msg = _normalise_str(data.get("msg")) or ""

    if card_n == 1 and msg:
        m_cycle = re.search(r"(?:CYCLE|REGIME)\s*:\s*([A-Za-z ]+)", msg, re.I)
        m_vol   = re.search(r"(?:VOL|VOLATILITY)\s*:\s*([A-Za-z ]+)", msg, re.I)

        if m_cycle:
            STATE["cycle"] = m_cycle.group(1).strip().upper()
        if m_vol:
            STATE["vol"] = m_vol.group(1).strip().upper()

        if not STATE.get("cycle") or not STATE.get("vol"):
            parts = msg.split()
            if len(parts) >= 2:
                STATE["cycle"] = (STATE.get("cycle") or parts[0]).upper()
                STATE["vol"]   = (STATE.get("vol") or parts[-1]).upper()

    elif card_n == 2 and msg:
        STATE["flow"] = msg

    elif card_n == 3:
        m = re.search(r"(\d+(\.\d+)?)\s*%", msg)
        if m:
            STATE["count"] = _clamp_int(int(float(m.group(1))), 0, 100)

        reg = _normalise_str(data.get("regime")) or _normalise_str(data.get("cycle"))
        if reg:
            STATE["cycle"] = reg.upper()

    elif card_n == 4:
        m = re.search(r"SAHM\s*:\s*([0-9]*\.?[0-9]+)", msg, re.I)
        if m:
            STATE["sahm"] = float(m.group(1))

    elif card_n in (5, 6, 7, 8, 9):
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
    with STATE_LOCK:
        snap = copy.deepcopy(STATE)
    return jsonify({
        "ok": True,
        "state": snap,
        "state_file": STATE_FILE,
        "state_file_exists": os.path.exists(STATE_FILE),
    }), 200

@app.route("/state", methods=["GET"])
def state():
    with STATE_LOCK:
        return jsonify(copy.deepcopy(STATE)), 200

@app.route("/debug.json")
def debug_json():
    with STATE_LOCK:
        snap = copy.deepcopy(STATE)
    with DEBUG_LOCK:
        logs = list(DEBUG_LOG)
    return jsonify({
        "state": snap,
        "debug": logs[:50],
        "server_ts": int(time.time() * 1000)
    })

@app.route("/debug")
def debug_page():
    return """
    <html>
    <head><title>Sutton House Debug</title></head>
    <body style="font-family: monospace">
    <h2>Sutton House – SCADA Debug</h2>
    <pre id="out">loading…</pre>
    <script>
      async function tick(){
        const r = await fetch('/debug.json');
        const j = await r.json();
        document.getElementById('out').textContent =
          JSON.stringify(j, null, 2);
      }
      tick();
      setInterval(tick, 3000);
    </script>
    </body>
    </html>
    """

# ============================================================
# INGEST MACRO (Python feeder endpoint)
# ============================================================
@app.route("/ingest_macro", methods=["POST"])
def ingest_macro():
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = _get_payload_any()
        meta = _extract_meta(data)

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "payload_not_object"}), 400

        # unwrap envelopes
        if isinstance(data.get("state"), dict):
            data = data["state"]
        elif isinstance(data.get("payload"), dict):
            data = data["payload"]
        elif isinstance(data.get("data"), dict):
            data = data["data"]

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "payload_not_object_after_unwrap"}), 400

        with STATE_LOCK:
            STATE["_server_ts"] = int(time.time() * 1000)

            # typed payloads first
            if "type" in data:
                try:
                    _parse_typed_payload(data)
                except Exception:
                    pass

            # legacy card-number payloads
            if "card" in data:
                _parse_card_payload(data)

            # field payloads
         

            # secret block
            if isinstance(data.get("secret"), dict):
                for sk in STATE["secret"].keys():
                    if sk in data["secret"]:
                        STATE["secret"][sk] = data["secret"][sk]

            _recompute_war_from_secret()

            try:
                _apply_sutton_house_normalisation()
            except Exception:
                pass

            _save_state_to_disk()
            payload = copy.deepcopy(STATE)

        socketio.emit("macro_update", payload)
        _log_debug("/ingest_macro", data, ok=True)
        return jsonify({"ok": True}), 200

    except Exception as e:
        _log_debug("/ingest_macro", {"error": str(e)}, ok=False)
        return jsonify({"ok": False, "error": "ingest_macro_failed", "detail": str(e)}), 400




# ============================================================
# WEBHOOK (TradingView direct)  ✅ KEEP ONE COPY ONLY
# Adds: STOCK LANES (WATCH + SCADA_STATUS) -> socket "stock_update"
# Also stores latest node payloads per ref_id for /node/<ref_id>
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    if not _authorised_webhook(request):
        abort(401)

    try:
        data = _get_payload_any()
        meta = _extract_meta(data)

        if not isinstance(data, dict):
            abort(400)

        # ----------------------------------------------------
        # unwrap common envelopes
        # ----------------------------------------------------
        if isinstance(data.get("state"), dict):
            data = data["state"]
        elif isinstance(data.get("payload"), dict):
            data = data["payload"]
        elif isinstance(data.get("data"), dict):
            data = data["data"]

        if not isinstance(data, dict):
            abort(400)

        with STATE_LOCK:
            # always stamp
            STATE["_server_ts"] = int(time.time() * 1000)

            # ====================================================
            # STOCK LANES (FAST PATH)
            # Accepts:
            #  {"type":"SCADA_STATUS", ...}
            #  {"type":"WATCH", ...}
            # Emits: socketio.emit("stock_update", msg)
            # Persists into STATE["stocks"] for warm start
            # ====================================================
            typ = str(data.get("type") or "").strip().upper()

            # Ensure storage exists (safe if you didn't add to STATE earlier)
            if "stocks" not in STATE or not isinstance(STATE.get("stocks"), dict):
                STATE["stocks"] = {"last_scada_by_ref": {}, "last_watch_by_ref": {}}
            if "last_scada_by_ref" not in STATE["stocks"]:
                STATE["stocks"]["last_scada_by_ref"] = {}
            if "last_watch_by_ref" not in STATE["stocks"]:
                STATE["stocks"]["last_watch_by_ref"] = {}

            if typ in ("SCADA_STATUS", "WATCH"):
                # normalize minimal fields
                try:
                    ref_id = data.get("ref_id")
                    ref_id = int(float(ref_id)) if ref_id is not None else None
                except Exception:
                    ref_id = None

                if ref_id is None:
                    abort(400)

                out = dict(data)
                out["type"] = typ
                out["ref_id"] = ref_id

                if out.get("ticker") is not None:
                    out["ticker"] = str(out["ticker"]).upper()

                # include server ts in this message too (helps comms/age)
                out["_server_ts"] = int(time.time() * 1000)

                # persist warm-start lanes
                if typ == "SCADA_STATUS":
                    STATE["stocks"]["last_scada_by_ref"][str(ref_id)] = out
                else:
                    STATE["stocks"]["last_watch_by_ref"][str(ref_id)] = out

                # ✅ ADD 3) store node payload for click-through debug page
                # (requires _store_node_payload helper + STATE["nodes"] support)
                # _store_node_payload(data)
                try:
                    _store_node_payload(out)
                except Exception:
                    pass

                _update_monitor_lane(_extract_meta(out))
                _save_state_to_disk()

                # emit stock-only update (do NOT spam macro_update)
                socketio.emit("stock_update", out)
                _log_debug("/webhook", out, ok=True)
                return "SUCCESS", 200

            # ------------------------------------------------
            # PINE AUTHORITY — MACRO + CARD4 (TRUTH)
            # Cards 1 & 3 MUST come from Pine MACRO payload
            # ------------------------------------------------
            pine_allow = {}

            # ----- Card 1: Regime + Vol (Pine truth)
            if "regime" in data:
                try:
                    pine_allow["regime"] = str(data["regime"]).upper()
                except Exception:
                    pass

            if "vol" in data:
                try:
                    pine_allow["vol"] = str(data["vol"]).upper()
                except Exception:
                    pass

            if "card1" in data:
                try:
                    pine_allow["card1"] = str(data["card1"])
                except Exception:
                    pass

            # ----- Card 3: Cycle clock (0–120 canonical)
            if "cycle" in data:
                try:
                    c = int(float(data["cycle"]))
                    if c < 0:
                        c = 0
                    if c > 120:
                        c = 120
                    pine_allow["cycle"] = c
                except Exception:
                    pass

            if "card3" in data:
                try:
                    pine_allow["card3"] = str(data["card3"])
                except Exception:
                    pass

            # ----- Optional: flow / rotation direction (server-side legacy)
            if "rot_dir" in data:
                try:
                    pine_allow["flow"] = str(data["rot_dir"])
                except Exception:
                    pass

            # ----- Card 4: Recession pulse (Pine truth)
            if "sahm" in data:
                try:
                    pine_allow["sahm"] = float(data["sahm"])
                except Exception:
                    pass

            if "spx_dd" in data:
                pine_allow["spx_dd"] = data["spx_dd"]
            elif "spxDrawdown" in data:
                pine_allow["spx_dd"] = data["spxDrawdown"]
            elif "dd" in data:
                pine_allow["spx_dd"] = data["dd"]
            elif "drawdown" in data:
                pine_allow["spx_dd"] = data["drawdown"]

            if pine_allow:
                STATE.update(pine_allow)

            # ------------------------------------------------
            # CARD 2 — CANONICAL (nested) ✅
            # ------------------------------------------------
            try:
                if "card2" not in STATE or not isinstance(STATE.get("card2"), dict):
                    STATE["card2"] = {"state": None, "text": None, "time": None, "tf": None, "ref_id": None}

                if typ == "CARD2":
                    st = data.get("state")
                    tx = data.get("text")

                    if st is not None:
                        STATE["card2"]["state"] = str(st).strip().upper()
                    if tx is not None:
                        STATE["card2"]["text"] = str(tx).strip()

                    for k in ("time", "tf", "ref_id"):
                        if k in data and data.get(k) not in (None, "", "NA", "na"):
                            STATE["card2"][k] = data.get(k)

                else:
                    c2 = data.get("card2")
                    if isinstance(c2, dict):
                        st = c2.get("state")
                        tx = c2.get("text")

                        if st is not None:
                            STATE["card2"]["state"] = str(st).strip().upper()
                        if tx is not None:
                            STATE["card2"]["text"] = str(tx).strip()

                        for k in ("time", "tf", "ref_id"):
                            if k in c2 and c2.get(k) not in (None, "", "NA", "na"):
                                STATE["card2"][k] = c2.get(k)

            except Exception:
                pass

            # ------------------------------------------------
            if "type" in data:
                try:
                    _parse_typed_payload(data)
                except Exception:
                    pass

            if "card" in data:
                try:
                    cn = _safe_int(data.get("card"))
                    if cn is None or cn != 2:
                        _parse_card_payload(data)
                except Exception:
                    pass

            _recompute_war_from_secret()
            _update_monitor_lane(meta)

            _save_state_to_disk()
            payload = copy.deepcopy(STATE)

        socketio.emit("macro_update", payload)
        _log_debug("/webhook", data, ok=True)
        return "SUCCESS", 200

    except Exception as e:
        _log_debug("/webhook", {"error": str(e)}, ok=False)
        return str(e), 400


@app.route("/node/<int:ref_id>", methods=["GET"])
def node_debug(ref_id: int):
    with STATE_LOCK:
        rec = (STATE.get("nodes") or {}).get("by_ref", {}).get(str(ref_id))

    if not rec:
        return f"<pre>NO DATA FOR ref_id={ref_id}</pre>", 200

    # tiny HTML page for commissioning
    pretty = json.dumps(rec, indent=2, ensure_ascii=False)
    return f"""
    <html>
      <head>
        <title>Node {ref_id} — Commissioning</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
      </head>
      <body style="background:#050505;color:#ddd;font-family:ui-monospace,Menlo,Consolas,monospace;padding:16px;">
        <h3 style="color:#BF953F;margin:0 0 12px 0;">NODE {ref_id} — COMMISSIONING</h3>
        <pre style="white-space:pre-wrap;word-break:break-word;line-height:1.35;">{pretty}</pre>
      </body>
    </html>
    """

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
