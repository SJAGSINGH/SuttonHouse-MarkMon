from flask import (
    Flask,
    request,
    render_template,
    send_from_directory,
    abort,
    jsonify,
    redirect,
    session,
    url_for,
    render_template_string,
)
from flask_socketio import SocketIO, emit
from functools import wraps
import os
import re
import time
import json
import atexit
import copy
import math
from threading import Lock
from typing import Any, Dict, Optional
from collections import deque
from datetime import datetime
# ============================================================
# SENTINEL LOGGING V1
# append-only JSONL event journal
# ============================================================
SENTINEL_LOG_FILE = os.environ.get("SENTINEL_LOG_FILE", "/var/data/sentinel_log.jsonl")
SENTINEL_LOG_FALLBACK = "/tmp/sentinel_log.jsonl"
SENTINEL_LOG_MAX_TAIL = 50
# -----------------------------------------
# Flask app must exist BEFORE using app.secret_key or @app.route
# -----------------------------------------
app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("WEBHOOK_SECRET", "dev-secret-key")

# ✅ Threading mode (works with Gunicorn gthreads)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# -----------------------------------------
# Temp Protection for Site
# -----------------------------------------
SITE_PASSWORD = os.environ.get("VAULT_PASSWORD", "changeme")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        password = request.form.get("password", "")

        if password == SITE_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        else:
            error = "Invalid password"

    return render_template_string("""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Sutton House Access</title>
      <style>
        body{
          margin:0;
          background:#0b0f14;
          color:#e6edf3;
          font-family:Arial,sans-serif;
          display:flex;
          align-items:center;
          justify-content:center;
          height:100vh;
        }
        .box{
          width:320px;
          padding:28px;
          background:#111827;
          border:1px solid #2a3441;
          border-radius:10px;
          text-align:center;
          box-shadow:0 0 30px rgba(0,0,0,.35);
        }
        h2{
          margin:0 0 8px 0;
          font-weight:600;
        }
        p{
          margin:0 0 18px 0;
          color:#9ca3af;
          font-size:14px;
        }
        input{
          width:100%;
          box-sizing:border-box;
          padding:12px;
          border-radius:8px;
          border:1px solid #374151;
          background:#0b0f14;
          color:#fff;
          outline:none;
        }
        button{
          width:100%;
          margin-top:12px;
          padding:12px;
          border:0;
          border-radius:8px;
          background:#22c55e;
          color:#08110b;
          font-weight:700;
          cursor:pointer;
        }
        .error{
          margin-top:12px;
          color:#ef4444;
          font-size:14px;
        }
      </style>
    </head>
    <body>
      <div class="box">
        <h2>Sutton House</h2>
        <p>Enter access code</p>
        <form method="post">
          <input type="password" name="password" placeholder="Password" required>
          <button type="submit">Enter</button>
        </form>
        {% if error %}
          <div class="error">{{ error }}</div>
        {% endif %}
      </div>
    </body>
    </html>
    """, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

VAULT_PASSWORD = (os.environ.get("VAULT_PASSWORD") or "").strip()

ATTEMPTS: Dict[str, list] = {}
ATTEMPT_WINDOW_SECS = 5 * 60
ATTEMPT_MAX = 6
STATE_LOCK = Lock()
# -----------------------------------------
# State save throttling (prevents disk spam)
# -----------------------------------------
SAVE_EVERY_MS = 2000  # save at most once every 2 seconds
_last_save_ms = 0
_last_save_lock = Lock()

def save_state_throttled():
    global _last_save_ms
    now = int(time.time() * 1000)
    with _last_save_lock:
        if now - _last_save_ms < SAVE_EVERY_MS:
            return
        _last_save_ms = now
    _save_state_to_disk()




# Unified state expected by index.html (+ secret block)
STATE: Dict[str, Any] = {
    "cycle": None,
    "vol": None,
    "flow": None,
    "count": None,
    "sahm": None,

    # ============================================================
    # MACRO V2 — EXTENDED LAYER (NON-DESTRUCTIVE TO V1)
    # ============================================================
    "macro_v2": {
        # ------------------------
        # Gold / Copper
        # ------------------------
        "gc": {
            "state": None,
            "trend_50sma": None,
            "msa_pct": None,
            "valid_signal": None,
            "explain": None,
        },

        # ------------------------
        # Gold / Silver
        # ------------------------
        "gs": {
            "state": None,
            "trend_50sma": None,
            "msa_pct": None,
            "valid_signal": None,
            "explain": None,
        },

        # ------------------------
        # Liquidity (WALCL)
        # ------------------------
        "walcl": {
            "state": None,
            "trend": None,
            "roc": None,
            "explain": None,
        },

        # ------------------------
        # FX Context (GBP lens only)
        # ------------------------
        "fx": {
            "gbpcad": {
                "state": None,
                "trend_50sma": None,
                "msa_pct": None,
            },
            "gbpaud": {
                "state": None,
                "trend_50sma": None,
                "msa_pct": None,
            },
            # derived layer
            "context": None,  # TAILWIND / HEADWIND / NEUTRAL
        },

        # ------------------------
        # Combined Commodity Internal State
        # ------------------------
        "internal": {
            "state": None,     # GOLD ALIGNMENT / GROWTH ALIGNMENT / TRANSITIONAL
            "explain": None,
        },

        # ------------------------
        # Cycle Phase (derived from 0–120 clock)
        # ------------------------
        "phase": {
            "id": None,
            "name": None,   # ACCUMULATION / EXPANSION / MATURATION / DISTRIBUTION
        },
    },

    # ============================================================
    # CARD 2 — canonical, nested ONLY
    # ============================================================
    "card2": {
        "state": None,
        "text": None,
        "time": None,
        "tf": None,
        "ref_id": None,
    },

    # ============================================================
    # MESSAGE SYSTEM — terminal stepper
    # ============================================================
    "message": {
        "setup": False,
        "trigger": "—",
        "ticker": "",
        "ref_id": None,
        "_ts": None,
    },

    # ============================================================
    # MONITORING / TELEMETRY
    # ============================================================
    "monitor": {
        "last_by_ref": {},
        "last_by_ticker": {},
        "last_hello": {},
    },

    "nodes": {
        "by_ref": {},
    },

    # ============================================================
    # MARKET ANCHORS
    # ============================================================
    "anchors": {
        "ASX": None,
        "LSE": None,
        "TSX": None,
        "NYSE": None,
    },

    # ============================================================
    # SECRET / INSTITUTIONAL LAYERS
    # ============================================================
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

import math

def _json_safe(x):
    """Recursively convert NaN/Inf to None so payload is valid JSON."""
    try:
        if isinstance(x, float):
            if math.isnan(x) or math.isinf(x):
                return None
    except Exception:
        pass

    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_json_safe(v) for v in x]
    if isinstance(x, tuple):
        return [_json_safe(v) for v in x]  # tuples -> lists for JSON

    return x

# ----------------------------
# Helpers
# ----------------------------
# ============================================================
# SENTINEL LOGGING HELPERS
# ============================================================
def _safe_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v, default=None):
    try:
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def _now_ms():
    return int(time.time() * 1000)


def _append_jsonl(filepath, obj):
    line = json.dumps(_json_safe(obj), separators=(",", ":"), ensure_ascii=False)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _append_sentinel_log(entry):
    """
    Append one JSONL row to the sentinel log.
    Falls back to /tmp if /var/data is unavailable.
    Also keeps a recent in-memory tail in STATE for future UI use.
    """
    if not isinstance(entry, dict):
        return

    entry = dict(entry)
    entry.setdefault("ts", _now_ms())
    entry.setdefault("source", "sentinel")
    entry.setdefault("ref_id", None)
    entry.setdefault("ticker", None)
    entry.setdefault("state", {})
    entry.setdefault("context", {})

    path = SENTINEL_LOG_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _append_jsonl(path, entry)
    except Exception:
        try:
            os.makedirs(os.path.dirname(SENTINEL_LOG_FALLBACK), exist_ok=True)
            _append_jsonl(SENTINEL_LOG_FALLBACK, entry)
            entry["context"] = dict(entry.get("context") or {})
            entry["context"]["log_fallback"] = True
        except Exception:
            return

    try:
        tail = STATE.get("sentinel_log_tail")
        if not isinstance(tail, list):
            tail = []
        tail.append(_json_safe(entry))
        if len(tail) > SENTINEL_LOG_MAX_TAIL:
            tail = tail[-SENTINEL_LOG_MAX_TAIL:]
        STATE["sentinel_log_tail"] = tail
    except Exception:
        pass


def _extract_secret_snapshot(data):
    """
    Pull the internal/secret panel values that matter for logging.
    Supports either top-level or nested secret keys.
    """
    data = data or {}
    secret = data.get("secret") if isinstance(data.get("secret"), dict) else {}

    def pick(*keys):
        for k in keys:
            if k in secret and secret.get(k) not in (None, ""):
                return secret.get(k)
            if k in data and data.get(k) not in (None, ""):
                return data.get(k)
        return None

    return {
        "indicator_x": _safe_int(pick("indicator_x", "ix", "x"), default=None),
        "indicator_y": _safe_int(pick("indicator_y", "iy", "y"), default=None),
        "panic_buy_ratio": _safe_float(pick("panic_buy_ratio", "panic_buy", "pbr"), default=None),
        "panic_sell_ratio": _safe_float(pick("panic_sell_ratio", "panic_sell", "psr"), default=None),
    }


def _extract_sentinel_macro_snapshot(data):
    """
    Canonical compact state snapshot for logging.
    Only fields Sentinel truly cares about.
    """
    data = data or {}

    secret_snap = _extract_secret_snapshot(data)

    cycle = (
        data.get("count")
        if data.get("count") is not None
        else data.get("cycle_count")
        if data.get("cycle_count") is not None
        else data.get("cycle")
    )

    phase = (
        data.get("phase_name")
        if data.get("phase_name") is not None
        else data.get("phase")
        if data.get("phase") is not None
        else data.get("phase_id")
    )

    return {
        "regime": data.get("regime"),
        "vol": data.get("vol") if data.get("vol") is not None else data.get("vol_state"),
        "flow": data.get("flow"),
        "cycle": _safe_int(cycle, default=None),
        "phase": phase,
        "sahm": _safe_float(data.get("sahm"), default=None),
        "s1_allowed": bool(data.get("s1_allowed")) if data.get("s1_allowed") is not None else None,
        "s2_allowed": bool(data.get("s2_allowed")) if data.get("s2_allowed") is not None else None,
        "s3_watch": bool(data.get("s3_watch")) if data.get("s3_watch") is not None else None,
        "s3_armed": bool(data.get("s3_armed")) if data.get("s3_armed") is not None else None,
        "s3_allowed": bool(data.get("s3_allowed")) if data.get("s3_allowed") is not None else None,
        "indicator_x": secret_snap["indicator_x"],
        "indicator_y": secret_snap["indicator_y"],
        "panic_buy_ratio": secret_snap["panic_buy_ratio"],
        "panic_sell_ratio": secret_snap["panic_sell_ratio"],
    }


def _extract_sentinel_node_snapshot(node):
    node = node or {}
    return {
        "ref_id": _safe_int(node.get("ref_id"), default=None),
        "ticker": (node.get("ticker") or "").upper() or None,
        "setup": bool(node.get("pill_setup_any") or node.get("setup") or node.get("setup_D") or node.get("setup_4H")),
        "signal": bool(
            node.get("signal_any")
            or node.get("trigger_any")
            or node.get("signal")
            or node.get("trigger")
        ),
        "event_active": bool(
            node.get("event_active")
            or node.get("event_window")
            or node.get("earnings_window")
            or node.get("dividend_window")
        ),
        "event_type": node.get("event_type"),
        "engine_d": node.get("setup_engine_D"),
        "engine_4h": node.get("setup_engine_4H"),
        "strategy": node.get("strategy"),
        "msa_ok": node.get("msa_ok"),
        "weekly_up": node.get("weekly_up"),
    }


def _get_monitor_nodes():
    """
    Attempts to gather current node objects from STATE.
    Adjust this if your canonical monitor structure differs.
    """
    out = []

    monitor = STATE.get("monitor")
    if isinstance(monitor, list):
        for x in monitor:
            if isinstance(x, dict):
                out.append(x)

    nodes = STATE.get("nodes")
    if isinstance(nodes, list):
        for x in nodes:
            if isinstance(x, dict):
                out.append(x)

    if isinstance(nodes, dict):
        for _, x in nodes.items():
            if isinstance(x, dict):
                out.append(x)

    seen = set()
    deduped = []
    for n in out:
        rid = _safe_int(n.get("ref_id"), default=None)
        key = (rid, (n.get("ticker") or "").upper())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)

    return deduped


def _compute_sentinel_cluster_flags():
    """
    Global operational summary for context logging.
    """
    any_setup = False
    any_signal = False
    active_setups = 0
    active_signals = 0

    for node in _get_monitor_nodes():
        snap = _extract_sentinel_node_snapshot(node)
        if snap["setup"]:
            any_setup = True
            active_setups += 1
        if snap["signal"]:
            any_signal = True
            active_signals += 1

    return {
        "any_setup": any_setup,
        "any_signal": any_signal,
        "active_setups": active_setups,
        "active_signals": active_signals,
    }


def _late_cycle_compression_on(state):
    state = state or {}
    cycle = _safe_int(state.get("cycle"), default=None)
    vol = str(state.get("vol") or "").upper()
    if cycle is None:
        return False
    return cycle >= 135 and vol == "COMPRESSION"

# ============================================================
# SENTINEL TRANSITION MEMORY
# ============================================================
_LAST_SENTINEL_MACRO = {}
_LAST_SENTINEL_NODE_STATES = {}
_LAST_SENTINEL_CLUSTER = {}
_LAST_SENTINEL_MESSAGE = None

def _log_macro_transition(code, message, severity="info", reason=None, state=None, extra_context=None):
    cluster = _compute_sentinel_cluster_flags()
    ctx = {
        "reason": reason or [],
        "any_setup": cluster["any_setup"],
        "any_signal": cluster["any_signal"],
        "active_setups": cluster["active_setups"],
        "active_signals": cluster["active_signals"],
    }
    if isinstance(extra_context, dict):
        ctx.update(extra_context)

    _append_sentinel_log({
        "kind": "MACRO_TRANSITION",
        "code": code,
        "severity": severity,
        "message": message,
        "state": state or {},
        "context": ctx,
    })


def process_sentinel_macro_logging(data):
    """
    Compare current macro-relevant state against last known macro state.
    Log only meaningful transitions.
    """
    global _LAST_SENTINEL_MACRO

    current = _extract_sentinel_macro_snapshot(data)
    prev = dict(_LAST_SENTINEL_MACRO or {})

    if not prev:
        _LAST_SENTINEL_MACRO = dict(current)
        return

    # regime
    if prev.get("regime") != current.get("regime"):
        _log_macro_transition(
            "REGIME_CHANGED",
            f"Macro regime changed: {prev.get('regime')} -> {current.get('regime')}",
            severity="attention",
            reason=["regime_changed"],
            state=current,
            extra_context={"from": prev.get("regime"), "to": current.get("regime")},
        )

    # vol
    if prev.get("vol") != current.get("vol"):
        _log_macro_transition(
            "VOL_CHANGED",
            f"Volatility state changed: {prev.get('vol')} -> {current.get('vol')}",
            severity="observe",
            reason=["vol_changed"],
            state=current,
            extra_context={"from": prev.get("vol"), "to": current.get("vol")},
        )

    # flow
    if prev.get("flow") != current.get("flow"):
        _log_macro_transition(
            "FLOW_CHANGED",
            f"Capital flow changed: {prev.get('flow')} -> {current.get('flow')}",
            severity="observe",
            reason=["flow_changed"],
            state=current,
            extra_context={"from": prev.get("flow"), "to": current.get("flow")},
        )

    # phase
    if prev.get("phase") != current.get("phase"):
        _log_macro_transition(
            "CYCLE_PHASE_CHANGED",
            f"Cycle phase changed: {prev.get('phase')} -> {current.get('phase')}",
            severity="attention",
            reason=["phase_changed"],
            state=current,
            extra_context={"from": prev.get("phase"), "to": current.get("phase")},
        )

    # s2
    if prev.get("s2_allowed") != current.get("s2_allowed"):
        _log_macro_transition(
            "S2_ALLOWED_ON" if current.get("s2_allowed") else "S2_ALLOWED_OFF",
            "Trend participation permitted. Conditions aligned with cycle phase."
            if current.get("s2_allowed")
            else "Trend participation no longer permitted under current macro conditions.",
            severity="info" if current.get("s2_allowed") else "attention",
            reason=["s2_permission_changed"],
            state=current,
        )

    # s1
    if prev.get("s1_allowed") != current.get("s1_allowed"):
        _log_macro_transition(
            "S1_ALLOWED_ON" if current.get("s1_allowed") else "S1_ALLOWED_OFF",
            "Contrarian observation framework permitted."
            if current.get("s1_allowed")
            else "Contrarian observation framework no longer permitted.",
            severity="observe",
            reason=["s1_permission_changed"],
            state=current,
        )

    # s3 watch
    if prev.get("s3_watch") != current.get("s3_watch"):
        _log_macro_transition(
            "S3_WATCH_ON" if current.get("s3_watch") else "S3_WATCH_OFF",
            "Recession watch status changed.",
            severity="attention" if current.get("s3_watch") else "observe",
            reason=["s3_watch_changed"],
            state=current,
        )

    # s3 armed
    if prev.get("s3_armed") != current.get("s3_armed"):
        _log_macro_transition(
            "S3_ARMED_ON" if current.get("s3_armed") else "S3_ARMED_OFF",
            "Recession framework armed."
            if current.get("s3_armed")
            else "Recession framework disarmed.",
            severity="critical" if current.get("s3_armed") else "attention",
            reason=["s3_armed_changed"],
            state=current,
        )

    # s3 allowed
    if prev.get("s3_allowed") != current.get("s3_allowed"):
        _log_macro_transition(
            "S3_ALLOWED_ON" if current.get("s3_allowed") else "S3_ALLOWED_OFF",
            "Recession participation permitted."
            if current.get("s3_allowed")
            else "Recession participation no longer permitted.",
            severity="critical" if current.get("s3_allowed") else "attention",
            reason=["s3_allowed_changed"],
            state=current,
        )

    # late cycle compression state
    prev_lcc = _late_cycle_compression_on(prev)
    curr_lcc = _late_cycle_compression_on(current)
    if prev_lcc != curr_lcc:
        _log_macro_transition(
            "LATE_CYCLE_COMPRESSION_ON" if curr_lcc else "LATE_CYCLE_COMPRESSION_OFF",
            "Late-cycle compression detected. Historical regime transition risk elevated."
            if curr_lcc
            else "Late-cycle compression condition cleared.",
            severity="critical" if curr_lcc else "observe",
            reason=["late_cycle_compression_changed"],
            state=current,
        )

    # secret pressure fields
    for fld, code_base, label in [
        ("indicator_x", "INDICATOR_X", "Indicator X"),
        ("indicator_y", "INDICATOR_Y", "Indicator Y"),
        ("panic_buy_ratio", "PANIC_BUY_RATIO", "Panic buy ratio"),
        ("panic_sell_ratio", "PANIC_SELL_RATIO", "Panic sell ratio"),
    ]:
        if prev.get(fld) != current.get(fld):
            _log_macro_transition(
                f"{code_base}_CHANGED",
                f"{label} changed: {prev.get(fld)} -> {current.get(fld)}",
                severity="observe",
                reason=[f"{fld}_changed"],
                state=current,
                extra_context={"from": prev.get(fld), "to": current.get(fld)},
            )

    _LAST_SENTINEL_MACRO = dict(current)

def _log_node_transition(kind, code, message, node_snap, severity="observe", extra_context=None):
    cluster = _compute_sentinel_cluster_flags()
    ctx = {
        "reason": [code.lower()],
        "any_setup": cluster["any_setup"],
        "any_signal": cluster["any_signal"],
        "active_setups": cluster["active_setups"],
        "active_signals": cluster["active_signals"],
    }
    if isinstance(extra_context, dict):
        ctx.update(extra_context)

    _append_sentinel_log({
        "kind": kind,
        "code": code,
        "severity": severity,
        "message": message,
        "ref_id": node_snap.get("ref_id"),
        "ticker": node_snap.get("ticker"),
        "state": {
            "setup": node_snap.get("setup"),
            "signal": node_snap.get("signal"),
            "event_active": node_snap.get("event_active"),
            "event_type": node_snap.get("event_type"),
            "engine_d": node_snap.get("engine_d"),
            "engine_4h": node_snap.get("engine_4h"),
            "strategy": node_snap.get("strategy"),
            "msa_ok": node_snap.get("msa_ok"),
            "weekly_up": node_snap.get("weekly_up"),
        },
        "context": ctx,
    })


def process_sentinel_node_logging(node):
    """
    Log node-level setup / signal / event transitions.
    """
    global _LAST_SENTINEL_NODE_STATES

    snap = _extract_sentinel_node_snapshot(node)
    ref_id = snap.get("ref_id")
    ticker = snap.get("ticker")
    if ref_id is None and not ticker:
        return

    key = f"{ref_id}:{ticker}"
    prev = dict(_LAST_SENTINEL_NODE_STATES.get(key) or {})

    if not prev:
        _LAST_SENTINEL_NODE_STATES[key] = dict(snap)
        return

    if prev.get("setup") != snap.get("setup"):
        _log_node_transition(
            "NODE_SETUP_ON" if snap.get("setup") else "NODE_SETUP_OFF",
            "SETUP_DETECTED" if snap.get("setup") else "SETUP_CLEARED",
            f"{ticker or 'NODE'} setup detected." if snap.get("setup") else f"{ticker or 'NODE'} setup cleared.",
            snap,
            severity="observe",
        )

    if prev.get("signal") != snap.get("signal"):
        _log_node_transition(
            "NODE_SIGNAL_ON" if snap.get("signal") else "NODE_SIGNAL_OFF",
            "SIGNAL_DETECTED" if snap.get("signal") else "SIGNAL_CLEARED",
            f"{ticker or 'NODE'} signal detected." if snap.get("signal") else f"{ticker or 'NODE'} signal cleared.",
            snap,
            severity="attention" if snap.get("signal") else "observe",
        )

    if prev.get("event_active") != snap.get("event_active"):
        evt = (snap.get("event_type") or "EVENT").upper()
        _log_node_transition(
            "EVENT_WINDOW_ON" if snap.get("event_active") else "EVENT_WINDOW_OFF",
            f"{evt}_WINDOW_ON" if snap.get("event_active") else f"{evt}_WINDOW_OFF",
            f"{ticker or 'NODE'} event window active ({evt})."
            if snap.get("event_active")
            else f"{ticker or 'NODE'} event window cleared ({evt}).",
            snap,
            severity="observe",
        )

    _LAST_SENTINEL_NODE_STATES[key] = dict(snap)

def process_sentinel_cluster_logging():
    global _LAST_SENTINEL_CLUSTER

    current = _compute_sentinel_cluster_flags()
    prev = dict(_LAST_SENTINEL_CLUSTER or {})

    if not prev:
        _LAST_SENTINEL_CLUSTER = dict(current)
        return

    if prev.get("any_setup") != current.get("any_setup"):
        _append_sentinel_log({
            "kind": "CLUSTER_CHANGE",
            "code": "FIRST_SETUP_ACTIVE" if current.get("any_setup") else "NO_ACTIVE_SETUPS",
            "severity": "observe",
            "message": "At least one monitored setup is active."
            if current.get("any_setup")
            else "No monitored setups remain active.",
            "state": _extract_sentinel_macro_snapshot(STATE),
            "context": current,
        })

    if prev.get("any_signal") != current.get("any_signal"):
        _append_sentinel_log({
            "kind": "CLUSTER_CHANGE",
            "code": "FIRST_SIGNAL_ACTIVE" if current.get("any_signal") else "NO_ACTIVE_SIGNALS",
            "severity": "attention" if current.get("any_signal") else "observe",
            "message": "At least one monitored signal is active."
            if current.get("any_signal")
            else "No monitored signals remain active.",
            "state": _extract_sentinel_macro_snapshot(STATE),
            "context": current,
        })

    if prev.get("active_setups") != current.get("active_setups") and current.get("active_setups", 0) >= 2:
        _append_sentinel_log({
            "kind": "CLUSTER_CHANGE",
            "code": "MULTI_NODE_SETUP_CLUSTER",
            "severity": "attention",
            "message": f"Multiple monitored setups active ({current.get('active_setups')}).",
            "state": _extract_sentinel_macro_snapshot(STATE),
            "context": current,
        })

    if prev.get("active_signals") != current.get("active_signals") and current.get("active_signals", 0) >= 2:
        _append_sentinel_log({
            "kind": "CLUSTER_CHANGE",
            "code": "MULTI_NODE_SIGNAL_CLUSTER",
            "severity": "attention",
            "message": f"Multiple monitored signals active ({current.get('active_signals')}).",
            "state": _extract_sentinel_macro_snapshot(STATE),
            "context": current,
        })

def log_sentinel_message(message_text, severity="info", reason=None, extra_context=None):
    """
    Log only when the actual emitted Sentinel message changes.
    """
    global _LAST_SENTINEL_MESSAGE

    msg = (message_text or "").strip()
    if not msg:
        return
    if msg == _LAST_SENTINEL_MESSAGE:
        return

    cluster = _compute_sentinel_cluster_flags()
    ctx = {
        "reason": reason or [],
        "any_setup": cluster["any_setup"],
        "any_signal": cluster["any_signal"],
        "active_setups": cluster["active_setups"],
        "active_signals": cluster["active_signals"],
    }
    if isinstance(extra_context, dict):
        ctx.update(extra_context)

    _append_sentinel_log({
        "kind": "SENTINEL_MESSAGE",
        "code": "MESSAGE_EMITTED",
        "severity": severity,
        "message": msg,
        "state": _extract_sentinel_macro_snapshot(STATE),
        "context": ctx,
    })

    _LAST_SENTINEL_MESSAGE = msg
    
    
    _LAST_SENTINEL_CLUSTER = dict(current)

def _phase_from_cycle(cycle_val: Optional[int]) -> Dict[str, Optional[Any]]:
    if cycle_val is None:
        return {"id": None, "name": None}

    try:
        c = int(cycle_val)
    except Exception:
        return {"id": None, "name": None}

    if 0 <= c < 30:
        return {"id": 1, "name": "ACCUMULATION"}
    if 30 <= c < 70:
        return {"id": 2, "name": "EXPANSION"}
    if 70 <= c < 100:
        return {"id": 3, "name": "MATURATION"}
    if 100 <= c <= 120:
        return {"id": 4, "name": "DISTRIBUTION"}

    return {"id": 5, "name": "RESET"}


def _derive_internal_state(gc_state: Optional[str], gs_state: Optional[str]) -> Dict[str, Optional[str]]:
    gc = (gc_state or "").strip().upper()
    gs = (gs_state or "").strip().upper()

    if gc == "STRENGTHENING" and gs == "STRENGTHENING":
        return {
            "state": "GOLD ALIGNMENT",
            "explain": "Gold is leading both copper and silver."
        }

    if gc == "WEAKENING" and gs == "WEAKENING":
        return {
            "state": "GROWTH ALIGNMENT",
            "explain": "Copper and silver are leading against gold."
        }

    if gc or gs:
        return {
            "state": "TRANSITIONAL",
            "explain": "Internal commodity leadership is mixed."
        }

    return {"state": None, "explain": None}


def _derive_fx_context(gbpcad_state: Optional[str], gbpaud_state: Optional[str]) -> Optional[str]:
    gc = (gbpcad_state or "").strip().upper()
    ga = (gbpaud_state or "").strip().upper()

    if gc == "WEAKENING" and ga == "WEAKENING":
        return "TAILWIND"
    if gc == "STRENGTHENING" and ga == "STRENGTHENING":
        return "HEADWIND"
    if gc or ga:
        return "NEUTRAL"
    return None

NODE_TYPES = {"SETUP", "SCADA_STATUS", "WATCH", "EVENT"}

def _store_node_payload(data: Dict[str, Any]) -> None:
    """
    Stores latest node-level payloads keyed by ref_id.
    Supports: SETUP / SCADA_STATUS / WATCH / EVENT
    Keeps last payload per type and per-type timestamps.
    """
    try:
        typ = str(data.get("type") or "").strip().upper()
        if typ not in NODE_TYPES:
            return

        ref = data.get("ref_id")
        if ref is None:
            return

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

        if "_ts" not in rec or not isinstance(rec.get("_ts"), dict):
            rec["_ts"] = {}

        # common convenience fields
        if data.get("ticker"):
            rec["ticker"] = str(data.get("ticker")).upper()
        rec["ref_id"] = ref_i

        if typ == "EVENT":
            rec["event"] = {
                "active": bool(data.get("event_active", False)),
                "type": str(data.get("event_type") or "none").strip().lower(),
                "state": str(data.get("event_state") or "none").strip().lower(),
                "days": data.get("event_days"),
                "event_ts": data.get("event_ts"),
                "earnings_ts": data.get("earnings_ts"),
                "div_ts": data.get("div_ts"),
                "pre_days": data.get("event_pre_days"),
                "post_days": data.get("event_post_days"),
                "label": data.get("event_label"),
                "_server_ts": now,
                "time": data.get("time"),
                "tf": data.get("tf"),
            }
            rec["_ts"]["event"] = now
        else:
            k = f"last_{typ.lower()}"
            rec[k] = data
            rec["_ts"][k] = now

        STATE["nodes"]["by_ref"][ref_key] = rec

    except Exception:
        return

def _handle_stock_payload(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalise + store a STOCK lane payload.
    Accepts only {"type":"SCADA_STATUS", ...} or {"type":"WATCH", ...}
    Stores into STATE["stocks"]["last_scada_by_ref"/"last_watch_by_ref"] keyed by ref_id (string).
    Returns the normalised payload dict (a new dict), or None if not a valid stock payload.
    """
    if not isinstance(msg, dict):
        return None

    typ = str(msg.get("type") or "").strip().upper()
    # ----------------------------
    # Market Anchor Updates
    # ----------------------------
    if typ == "ANCHOR_UPDATE":
        market = str(msg.get("market_id") or "").upper()

        if market in STATE["anchors"]:
            STATE["anchors"][market] = msg
            STATE["_server_ts"] = int(time.time() * 1000)

        return None
    
    if typ not in ("SCADA_STATUS", "WATCH"):
        return None

    ref_raw = msg.get("ref_id")
    if ref_raw is None:
        return None

    try:
        ref_id = int(float(ref_raw))
    except Exception:
        return None

    out = dict(msg)  # don't mutate caller's dict
    out["type"] = typ
    out["ref_id"] = ref_id

    if out.get("ticker") is not None:
        out["ticker"] = str(out["ticker"]).strip().upper()

    # server timestamp passthrough / stamp
    out["_server_ts"] = int(out.get("_server_ts") or (time.time() * 1000))

    # ensure storage exists (prevents KeyError)
    if "stocks" not in STATE or not isinstance(STATE.get("stocks"), dict):
        STATE["stocks"] = {"last_scada_by_ref": {}, "last_watch_by_ref": {}}
    if "last_scada_by_ref" not in STATE["stocks"] or not isinstance(STATE["stocks"].get("last_scada_by_ref"), dict):
        STATE["stocks"]["last_scada_by_ref"] = {}
    if "last_watch_by_ref" not in STATE["stocks"] or not isinstance(STATE["stocks"].get("last_watch_by_ref"), dict):
        STATE["stocks"]["last_watch_by_ref"] = {}

    # store
    key = str(ref_id)
    if typ == "SCADA_STATUS":
        STATE["stocks"]["last_scada_by_ref"][key] = out
    else:
        STATE["stocks"]["last_watch_by_ref"][key] = out

    return out

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
def _truthy(v) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    s = str(v).strip().upper()
    return s in ("1", "TRUE", "ON", "YES", "ACTIVE")

def _derive_message_from_scada(out: Dict[str, Any], prev_msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the canonical Message System lane from a SCADA_STATUS payload.
    Uses your existing fields: setup_any / pill_setup_any / trigger_any / signal.
    """
    ticker = str(out.get("ticker") or "").strip().upper()

    ref_id = out.get("ref_id")
    try:
        ref_id = int(ref_id) if ref_id is not None else None
    except Exception:
        ref_id = None

    setup_on = _truthy(out.get("setup_any")) or _truthy(out.get("pill_setup_any"))
    # Treat "signal" as trigger truth too (your Pine includes signal_any -> "signal")
    trigger_on = _truthy(out.get("trigger_any")) or _truthy(out.get("signal")) or _truthy(out.get("signal_any"))
    trig_text = "TRIGGER" if trigger_on else "—"

    now = int(time.time() * 1000)

    # If setup/trigger is ON for this ref/ticker, promote it to the message lane
    if setup_on or trigger_on:
        return {
            "setup": bool(setup_on),
            "trigger": trig_text,
            "ticker": ticker,
            "ref_id": ref_id,
            "_ts": now,
        }

    # Otherwise: only clear if we were tracking THIS ref/ticker
    cur_ref = prev_msg.get("ref_id")
    cur_tkr = str(prev_msg.get("ticker") or "").strip().upper()
    if (ref_id is not None and cur_ref == ref_id) or (ticker and cur_tkr == ticker):
        return {
            "setup": False,
            "trigger": "—",
            "ticker": ticker or "",
            "ref_id": ref_id,
            "_ts": now,
        }

    # No change
    return prev_msg

def _normalise_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
def _truthy(v) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    s = str(v).strip().upper()
    return s in ("1", "TRUE", "ON", "YES", "ACTIVE")

def _update_message_from_scada(out: Dict[str, Any]) -> None:
    """
    Update STATE['message'] using SCADA_STATUS truth.
    Priority:
      - if signal/trigger present: step to signal
      - else if setup present: step to setup
      - else if previously tracking this ref/ticker, clear
    """
    if str(out.get("type") or "").upper() != "SCADA_STATUS":
        return

    ticker = str(out.get("ticker") or "").strip().upper()
    try:
        ref_id = int(out.get("ref_id")) if out.get("ref_id") is not None else None
    except Exception:
        ref_id = None

    setup_on   = _truthy(out.get("setup_any")) or _truthy(out.get("pill_setup_any"))
    trigger_on = _truthy(out.get("trigger_any")) or _truthy(out.get("signal")) or _truthy(out.get("signal_any"))
    # If you have a human-readable trigger string, map it here; otherwise dash
    trig_text = "TRIGGER" if trigger_on else "—"

    now = int(time.time() * 1000)

    # If this payload indicates setup/trigger, it becomes the active message lane.
    if trigger_on or setup_on:
        STATE.setdefault("message", {})
        STATE["message"].update({
            "setup": bool(setup_on),
            "trigger": trig_text,
            "ticker": ticker,
            "ref_id": ref_id,
            "_ts": now,
        })
        return

    # Otherwise, only clear message lane if it was tracking this same ref/ticker.
    cur = STATE.get("message") or {}
    if (ref_id is not None and cur.get("ref_id") == ref_id) or (ticker and cur.get("ticker") == ticker):
        STATE["message"].update({
            "setup": False,
            "trigger": "—",
            "ticker": ticker or "",
            "ref_id": ref_id,
            "_ts": now,
        })
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

    # 1️⃣ Direct JSON body
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data

    # 2️⃣ Form-encoded body
    if request.form:
        d = dict(request.form)

        # If single form field that itself contains JSON
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

    # 3️⃣ Raw body fallback
    raw = (request.data or b"").decode("utf-8", errors="ignore").strip()

    if raw:
        if raw[0] in "{[":
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception as e:
                raise ValueError(f"Bad JSON body: {repr(e)} :: {raw[:500]}")

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
            # -----------------------------------------
            # Core macro state
            # -----------------------------------------
            for k in ("cycle", "vol", "flow", "count", "sahm", "monitor", "_server_ts"):
                if k in cached:
                    STATE[k] = cached.get(k)

            # -----------------------------------------
            # Macro V2
            # -----------------------------------------
            if isinstance(cached.get("macro_v2"), dict):
                STATE["macro_v2"] = cached.get("macro_v2")

            # -----------------------------------------
            # Card 2
            # -----------------------------------------
            if isinstance(cached.get("card2"), dict):
                STATE["card2"] = cached.get("card2")

            # -----------------------------------------
            # Message system
            # -----------------------------------------
            if isinstance(cached.get("message"), dict):
                STATE["message"] = cached.get("message")

            # -----------------------------------------
            # Stock lanes (warm start)
            # -----------------------------------------
            if isinstance(cached.get("stocks"), dict):
                STATE["stocks"] = cached.get("stocks")

            # -----------------------------------------
            # Node click-through/debug payloads
            # -----------------------------------------
            if isinstance(cached.get("nodes"), dict):
                STATE["nodes"] = cached.get("nodes")

            # -----------------------------------------
            # Market anchors
            # -----------------------------------------
            if isinstance(cached.get("anchors"), dict):
                STATE["anchors"] = cached.get("anchors")

            # -----------------------------------------
            # Secret block
            # -----------------------------------------
            if isinstance(cached.get("secret"), dict):
                for sk in STATE["secret"]:
                    STATE["secret"][sk] = cached["secret"].get(sk)

    except Exception as e:
        print("State load error:", e)

def _save_state_to_disk() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"

        with STATE_LOCK:
            snap = copy.deepcopy(STATE)

        snap = _json_safe(snap)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)

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

def _bootstrap_sentinel_logging_memory_from_state():
    global _LAST_SENTINEL_MACRO, _LAST_SENTINEL_NODE_STATES, _LAST_SENTINEL_CLUSTER

    try:
        _LAST_SENTINEL_MACRO = _extract_sentinel_macro_snapshot(STATE)
    except Exception:
        _LAST_SENTINEL_MACRO = {}

    try:
        _LAST_SENTINEL_NODE_STATES = {}
        for node in _get_monitor_nodes():
            snap = _extract_sentinel_node_snapshot(node)
            key = f"{snap.get('ref_id')}:{snap.get('ticker')}"
            _LAST_SENTINEL_NODE_STATES[key] = snap
    except Exception:
        _LAST_SENTINEL_NODE_STATES = {}

    try:
        _LAST_SENTINEL_CLUSTER = _compute_sentinel_cluster_flags()
    except Exception:
        _LAST_SENTINEL_CLUSTER = {}




def _apply_macro_v2_normalisation() -> None:
    """
    Derives combined Macro V2 state from raw V2 lanes.
    Does NOT overwrite Macro V1 core fields.
    """
    mv2 = STATE.setdefault("macro_v2", {})

    gc = mv2.setdefault("gc", {})
    gs = mv2.setdefault("gs", {})
    walcl = mv2.setdefault("walcl", {})
    fx = mv2.setdefault("fx", {"gbpcad": {}, "gbpaud": {}, "context": None})
    internal = mv2.setdefault("internal", {"state": None, "explain": None})

    # Phase from cycle (0..120 canonical)
    phase = _phase_from_cycle(_safe_int(STATE.get("cycle")))
    mv2["phase"] = phase

    # Combined commodity internal state
    internal_state = _derive_internal_state(gc.get("state"), gs.get("state"))
    mv2["internal"] = internal_state

    # FX context
    fx["context"] = _derive_fx_context(
        (fx.get("gbpcad") or {}).get("state"),
        (fx.get("gbpaud") or {}).get("state"),
    )
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
_bootstrap_sentinel_logging_memory_from_state()

@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)



    
@app.get("/health")
def health():
    # Must be constant-time and never block
    return jsonify({"ok": True}), 200

@app.get("/health/snapshot")
def health_snapshot():
    with STATE_LOCK:
        snap = copy.deepcopy(STATE)

    snap = _json_safe(snap)

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

    snap = _json_safe(snap)
    logs = _json_safe(logs)

    return jsonify({
        "state": snap,
        "debug": logs[:50],
        "server_ts": int(time.time() * 1000)
    })

from flask import Response

@app.route("/debug")
def debug_page():
    return Response("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Sutton House Debug</title>
  <style>
    body { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; margin: 16px; }
    .bar { display:flex; gap:12px; align-items:center; margin-bottom:12px; }
    .pill { padding: 2px 8px; border-radius: 999px; border: 1px solid #444; font-size: 12px; }
    .ok { color: #0a0; border-color:#0a0; }
    .bad { color: #c00; border-color:#c00; }
    pre { white-space: pre-wrap; word-break: break-word; padding: 12px; border: 1px solid #333; border-radius: 8px; }
    a { color:#6af; }
  </style>
</head>
<body>
  <div class="bar">
    <div class="pill" id="status">loading</div>
    <div class="pill" id="meta">—</div>
    <a href="/debug.json" target="_blank">open /debug.json</a>
  </div>

  <pre id="out">loading…</pre>

  <script>
    function setStatus(ok, text) {
      const el = document.getElementById('status');
      el.textContent = text;
      el.className = 'pill ' + (ok ? 'ok' : 'bad');
    }

    async function tick(){
      const out = document.getElementById('out');
      const meta = document.getElementById('meta');

      try {
        const r = await fetch('/debug.json', { cache: 'no-store' });
        const ct = (r.headers.get('content-type') || '').toLowerCase();
        const txt = await r.text();

        meta.textContent = `HTTP ${r.status} • ${ct || 'no content-type'} • ${new Date().toLocaleTimeString()}`;

        // Try JSON parse only if it looks like JSON
        const trimmed = (txt || '').trim();
        const looksJson = trimmed.startsWith('{') || trimmed.startsWith('[');

        if (r.ok && looksJson) {
          try {
            const j = JSON.parse(trimmed);
            out.textContent = JSON.stringify(j, null, 2);
            setStatus(true, 'OK');
            return;
          } catch (e) {
            setStatus(false, 'BAD JSON (parse failed)');
            out.textContent = `JSON.parse failed: ${e}\\n\\n--- raw ---\\n${txt}`;
            return;
          }
        }

        // Non-JSON or non-200: show raw response (often 502 HTML)
        setStatus(false, r.ok ? 'NON-JSON RESPONSE' : 'HTTP ERROR');
        out.textContent = `Non-JSON or error response\\n\\n--- raw ---\\n${txt}`;

      } catch (e) {
        setStatus(false, 'FETCH FAILED');
        out.textContent = `Fetch failed: ${e}`;
      }
    }

    tick();
    setInterval(tick, 3000);
  </script>
</body>
</html>
""", mimetype="text/html")

# ============================================================
# INGEST MACRO (Python feeder endpoint)
# ============================================================
@app.route("/ingest_macro", methods=["POST"])
def ingest_macro():
    data = request.get_json(silent=True) or {}

    try:
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

            payload = copy.deepcopy(STATE)

        # OUTSIDE LOCK
        save_state_throttled()
        socketio.emit("macro_update", _json_safe(payload))

        _log_debug("/ingest_macro", data, ok=True)
        return jsonify({"ok": True}), 200

    except Exception as e:
        _log_debug("/ingest_macro", {"error": str(e)}, ok=False)
        return jsonify({"ok": False, "error": "ingest_macro_failed", "detail": str(e)}), 400

DAY_MS = 24 * 60 * 60 * 1000


def _safe_int_or_none(v):
    try:
        if v in (None, "", "NA", "na", "null"):
            return None
        return int(float(v))
    except Exception:
        return None


def _event_state_from_ts(event_ts_ms, now_ms, pre_days=5, post_days=5):
    """
    Server-authoritative event lifecycle.
    Returns: active, state, days
      state in {"pre","today","post","none"}
      days:
        pre  -> positive days until event
        today-> 0
        post -> positive days since event
    """
    if not event_ts_ms:
        return False, "none", None

    delta_ms = int(event_ts_ms) - int(now_ms)

    # same calendar-day style tolerance
    if abs(delta_ms) < DAY_MS:
        return True, "today", 0

    if delta_ms > 0:
        days_to = int(math.ceil(delta_ms / DAY_MS))
        if days_to <= int(pre_days):
            return True, "pre", days_to
        return False, "none", days_to

    days_since = int(math.floor(abs(delta_ms) / DAY_MS))
    if days_since <= int(post_days):
        return True, "post", days_since

    return False, "none", days_since

def _refresh_node_event_states() -> None:
    """
    Recompute latched node events against current server time.
    This allows pre/today/post to progress even when Pine is silent.
    """
    try:
        nodes = ((STATE.get("nodes") or {}).get("by_ref") or {})
        if not isinstance(nodes, dict):
            return

        now_ms = int(time.time() * 1000)

        for ref_key, rec in nodes.items():
            if not isinstance(rec, dict):
                continue

            ev = rec.get("event")
            if not isinstance(ev, dict):
                continue

            event_ts = _safe_int_or_none(ev.get("event_ts"))
            if not event_ts:
                continue

            pre_days = _safe_int_or_none(ev.get("pre_days"))
            post_days = _safe_int_or_none(ev.get("post_days"))
            if pre_days is None:
                pre_days = 5
            if post_days is None:
                post_days = 5

            active, state, days = _event_state_from_ts(
                event_ts, now_ms, pre_days, post_days
            )

            ev["active"] = bool(active)
            ev["state"] = state
            ev["days"] = days
            ev["_server_ts"] = now_ms

            ev_type = str(ev.get("type") or "event").strip().lower()
            if ev_type == "earnings":
                base_label = "EARNINGS"
            elif ev_type == "dividend":
                base_label = "DIVIDEND"
            else:
                base_label = "EVENT"

            if state == "today":
                ev["label"] = f"{base_label} • TODAY"
            elif state == "pre" and days is not None:
                ev["label"] = f"{base_label} • PRE • {days}D"
            elif state == "post" and days is not None:
                ev["label"] = f"{base_label} • POST • {days}D"
            else:
                ev["label"] = base_label

    except Exception:
        return


def _process_event_payload(data: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Process one EVENT payload, latch event_ts, compute server-side lifecycle,
    persist into STATE["nodes"]["by_ref"][ref_id]["event"], and return the
    canonical outbound EVENT payload.
    """
    try:
        ref_id = data.get("ref_id")
        ref_id = int(float(ref_id)) if ref_id is not None else None
    except Exception:
        ref_id = None

    if ref_id is None:
        return None

    now_ms = int(time.time() * 1000)

    out = dict(data)
    out["type"] = "EVENT"
    out["ref_id"] = ref_id

    if out.get("ticker") is not None:
        out["ticker"] = str(out["ticker"]).upper()

    out["_server_ts"] = now_ms

    incoming_event_type = str(out.get("event_type") or "none").strip().lower()
    incoming_earnings_ts = _safe_int_or_none(out.get("earnings_ts"))
    incoming_div_ts = _safe_int_or_none(out.get("div_ts"))

    event_pre_days = _safe_int_or_none(out.get("event_pre_days"))
    event_post_days = _safe_int_or_none(out.get("event_post_days"))
    if event_pre_days is None:
        event_pre_days = 5
    if event_post_days is None:
        event_post_days = 5

    if "nodes" not in STATE or not isinstance(STATE.get("nodes"), dict):
        STATE["nodes"] = {"by_ref": {}}
    if "by_ref" not in STATE["nodes"] or not isinstance(STATE["nodes"].get("by_ref"), dict):
        STATE["nodes"]["by_ref"] = {}

    ref_key = str(ref_id)
    node_rec = STATE["nodes"]["by_ref"].get(ref_key)
    if not isinstance(node_rec, dict):
        node_rec = {}

    existing_event = node_rec.get("event")
    if not isinstance(existing_event, dict):
        existing_event = {}

    incoming_event_ts = incoming_earnings_ts or incoming_div_ts

    latched_event_ts = _safe_int_or_none(existing_event.get("event_ts"))
    latched_type = str(existing_event.get("type") or "none").strip().lower()

    if latched_event_ts:
        _, prev_state, _ = _event_state_from_ts(
            latched_event_ts, now_ms, event_pre_days, event_post_days
        )
    else:
        prev_state = "none"

    use_event_ts = latched_event_ts
    use_event_type = latched_type if latched_type != "none" else incoming_event_type

    if incoming_event_ts:
        if not latched_event_ts:
            use_event_ts = incoming_event_ts
            use_event_type = incoming_event_type
        elif incoming_event_ts == latched_event_ts:
            use_event_ts = incoming_event_ts
            use_event_type = incoming_event_type
        elif prev_state == "none":
            use_event_ts = incoming_event_ts
            use_event_type = incoming_event_type

    event_active, server_event_state, server_event_days = _event_state_from_ts(
        use_event_ts, now_ms, event_pre_days, event_post_days
    )

    if (
        server_event_state == "none"
        and use_event_ts
        and incoming_event_ts
        and incoming_event_ts != use_event_ts
    ):
        use_event_ts = incoming_event_ts
        use_event_type = incoming_event_type
        event_active, server_event_state, server_event_days = _event_state_from_ts(
            use_event_ts, now_ms, event_pre_days, event_post_days
        )

    out["event_type"] = use_event_type if use_event_type != "none" else incoming_event_type
    out["event_state"] = server_event_state
    out["event_active"] = bool(event_active)
    out["event_days"] = server_event_days
    out["event_pre_days"] = event_pre_days
    out["event_post_days"] = event_post_days
    out["earnings_ts"] = incoming_earnings_ts
    out["div_ts"] = incoming_div_ts
    out["event_ts"] = use_event_ts

    if out["event_type"] == "earnings":
        base_label = "EARNINGS"
    elif out["event_type"] == "dividend":
        base_label = "DIVIDEND"
    else:
        base_label = "EVENT"

    if server_event_state == "today":
        out["event_label"] = f"{base_label} • TODAY"
    elif server_event_state == "pre" and server_event_days is not None:
        out["event_label"] = f"{base_label} • PRE • {server_event_days}D"
    elif server_event_state == "post" and server_event_days is not None:
        out["event_label"] = f"{base_label} • POST • {server_event_days}D"
    else:
        out["event_label"] = base_label

    node_rec["ticker"] = out.get("ticker") or node_rec.get("ticker")
    node_rec["ref_id"] = ref_id
    node_rec["event"] = {
        "active": bool(out["event_active"]),
        "type": out["event_type"],
        "state": out["event_state"],
        "days": out["event_days"],
        "event_ts": out["event_ts"],
        "earnings_ts": out["earnings_ts"],
        "div_ts": out["div_ts"],
        "pre_days": out["event_pre_days"],
        "post_days": out["event_post_days"],
        "label": out["event_label"],
        "_server_ts": now_ms,
        "time": out.get("time"),
        "tf": out.get("tf"),
    }

    ts_map = node_rec.get("_ts")
    if not isinstance(ts_map, dict):
        ts_map = {}
    ts_map["event"] = now_ms
    node_rec["_ts"] = ts_map

    STATE["nodes"]["by_ref"][ref_key] = node_rec
    return out
    
# ============================================================
# WEBHOOK (TradingView direct)  ✅ KEEP ONE COPY ONLY
# Adds: STOCK LANES (WATCH + SCADA_STATUS + EVENT) -> socket "stock_update"
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

        # We'll decide after lock whether to save + what to emit
        do_save = False
        emit_event = None
        emit_payload = None

        # optional second emit (for message lane transitions)
        emit_event2 = None
        emit_payload2 = None

        # ====================================================
        # STATE MUTATION ONLY (LOCK IS TINY)
        # ====================================================
        with STATE_LOCK:
            # always stamp
            STATE["_server_ts"] = int(time.time() * 1000)
            _refresh_node_event_states()   # ✅ ADD THIS HERE
            typ = str(data.get("type") or "").strip().upper()

            # ----------------------------------------------------
            # Ensure storage exists (warm start lanes)
            # ----------------------------------------------------
            if "stocks" not in STATE or not isinstance(STATE.get("stocks"), dict):
                STATE["stocks"] = {"last_scada_by_ref": {}, "last_watch_by_ref": {}}
            if "last_scada_by_ref" not in STATE["stocks"]:
                STATE["stocks"]["last_scada_by_ref"] = {}
            if "last_watch_by_ref" not in STATE["stocks"]:
                STATE["stocks"]["last_watch_by_ref"] = {}

            # ----------------------------------------------------
            # Ensure Message System lane exists (terminal stepper)
            # ----------------------------------------------------
            if "message" not in STATE or not isinstance(STATE.get("message"), dict):
                STATE["message"] = {
                    "setup": False,
                    "trigger": "—",
                    "ticker": "",
                    "ref_id": None,
                    "_ts": None,
                }

            # ----------------------------------------------------
            # Ensure Market Anchors lane exists
            # ----------------------------------------------------
            if "anchors" not in STATE or not isinstance(STATE.get("anchors"), dict):
                STATE["anchors"] = {
                    "ASX": None,
                    "LSE": None,
                    "TSX": None,
                    "NYSE": None,
                }

            # ----------------------------------------------------
            # Ensure Macro V2 lane exists
            # ----------------------------------------------------
            if "macro_v2" not in STATE or not isinstance(STATE.get("macro_v2"), dict):
                STATE["macro_v2"] = {
                    "gc": {
                        "state": None,
                        "trend_50sma": None,
                        "msa_pct": None,
                        "valid_signal": None,
                        "explain": None,
                    },
                    "gs": {
                        "state": None,
                        "trend_50sma": None,
                        "msa_pct": None,
                        "valid_signal": None,
                        "explain": None,
                    },
                    "walcl": {
                        "state": None,
                        "trend": None,
                        "roc": None,
                        "explain": None,
                    },
                    "fx": {
                        "gbpcad": {
                            "state": None,
                            "trend_50sma": None,
                            "msa_pct": None,
                        },
                        "gbpaud": {
                            "state": None,
                            "trend_50sma": None,
                            "msa_pct": None,
                        },
                        "context": None,
                    },
                    "internal": {
                        "state": None,
                        "explain": None,
                    },
                    "phase": {
                        "id": None,
                        "name": None,
                    },
                }

            # ----------------------------------------------------
            # Local helpers for Macro V2 derivations
            # ----------------------------------------------------
            def _phase_from_cycle(cycle_val):
                try:
                    c = int(float(cycle_val))
                except Exception:
                    return {"id": None, "name": None}

                if 0 <= c < 30:
                    return {"id": 1, "name": "ACCUMULATION"}
                if 30 <= c < 70:
                    return {"id": 2, "name": "EXPANSION"}
                if 70 <= c < 100:
                    return {"id": 3, "name": "MATURATION"}
                if 100 <= c <= 120:
                    return {"id": 4, "name": "DISTRIBUTION"}
                return {"id": 5, "name": "RESET"}

            def _derive_internal_state(gc_state, gs_state):
                gc = str(gc_state or "").strip().upper()
                gs = str(gs_state or "").strip().upper()

                if gc == "STRENGTHENING" and gs == "STRENGTHENING":
                    return {
                        "state": "GOLD ALIGNMENT",
                        "explain": "Gold is leading both copper and silver."
                    }

                if gc == "WEAKENING" and gs == "WEAKENING":
                    return {
                        "state": "GROWTH ALIGNMENT",
                        "explain": "Copper and silver are leading against gold."
                    }

                if gc or gs:
                    return {
                        "state": "TRANSITIONAL",
                        "explain": "Internal commodity leadership is mixed."
                    }

                return {"state": None, "explain": None}

            def _derive_fx_context(gbpcad_state, gbpaud_state):
                gc = str(gbpcad_state or "").strip().upper()
                ga = str(gbpaud_state or "").strip().upper()

                if gc == "WEAKENING" and ga == "WEAKENING":
                    return "TAILWIND"
                if gc == "STRENGTHENING" and ga == "STRENGTHENING":
                    return "HEADWIND"
                if gc or ga:
                    return "NEUTRAL"
                return None

            def _apply_macro_v2_normalisation():
                mv2 = STATE.setdefault("macro_v2", {})

                gc = mv2.setdefault("gc", {})
                gs = mv2.setdefault("gs", {})
                walcl = mv2.setdefault("walcl", {})
                fx = mv2.setdefault("fx", {"gbpcad": {}, "gbpaud": {}, "context": None})

                # phase from cycle
                mv2["phase"] = _phase_from_cycle(STATE.get("cycle"))

                # combined commodity internal state
                mv2["internal"] = _derive_internal_state(
                    gc.get("state"),
                    gs.get("state"),
                )

                # fx context
                fx["context"] = _derive_fx_context(
                    (fx.get("gbpcad") or {}).get("state"),
                    (fx.get("gbpaud") or {}).get("state"),
                )

            # ====================================================
            # MARKET ANCHOR FAST PATH
            # ref_id transport ids:
            # 101 = ASX master (XJO)
            # 105 = LSE master
            # 106 = TSX master
            # 107 = NYSE master
            # ====================================================
            if typ == "ANCHOR_UPDATE":
                try:
                    ref_id = data.get("ref_id")
                    ref_id = int(float(ref_id)) if ref_id is not None else None
                except Exception:
                    ref_id = None

                market = None
                if ref_id == 101:
                    market = "ASX"
                elif ref_id == 105:
                    market = "LSE"
                elif ref_id == 106:
                    market = "TSX"
                elif ref_id == 107:
                    market = "NYSE"

                if market:
                    anchor_out = dict(data)
                    anchor_out["type"] = "ANCHOR_UPDATE"
                    anchor_out["ref_id"] = ref_id
                    anchor_out["market_id"] = market
                    anchor_out["_server_ts"] = int(time.time() * 1000)

                    if anchor_out.get("ticker") is not None:
                        anchor_out["ticker"] = str(anchor_out["ticker"]).upper()

                    STATE["anchors"][market] = anchor_out

                    _update_monitor_lane(_extract_meta(anchor_out))

                    do_save = True
                    emit_event = "macro_update"
                    emit_payload = copy.deepcopy(STATE)

                else:
                    abort(400)

            # ====================================================
            # EVENT BATCH LANE
            # Accepts:
            #   {"type":"EVENT_BATCH","events":[{EVENT...},{EVENT...}]}
            # Processes each child as a normal EVENT.
            # Emits one stock_update per child so frontend stays unchanged.
            # ====================================================
            elif typ == "EVENT_BATCH":
                events = data.get("events") or []
                if not isinstance(events, list):
                    abort(400)

                emitted_any = False

                for ev in events:
                    if not isinstance(ev, dict):
                        continue

                    ev_data = dict(ev)
                    ev_data["type"] = "EVENT"

                    out = _process_event_payload(ev_data)
                    if out is None:
                        continue

                    try:
                        _update_monitor_lane(_extract_meta(out))
                    except Exception:
                        pass

                    try:
                        socketio.emit("stock_update", _json_safe(out))
                    except Exception:
                        pass

                    emitted_any = True

                if emitted_any:
                    do_save = True
                else:
                    abort(400)

            # ====================================================
            # EVENT LANE
            # ====================================================
            elif typ == "EVENT":
                out = _process_event_payload(data)
                if out is None:
                    abort(400)

                _update_monitor_lane(_extract_meta(out))

                do_save = True
                emit_event = "stock_update"
                emit_payload = out
            # ====================================================
            # STOCK LANES (FAST PATH)
            # Accepts:
            #  {"type":"SCADA_STATUS", ...}
            #  {"type":"WATCH", ...}
            # Emits: socketio.emit("stock_update", msg)
            # Persists into STATE["stocks"] for warm start
            # ====================================================
            elif typ in ("SCADA_STATUS", "WATCH"):
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

                # ----------------------------------------------------
                # MARKET DETECTION (for anchor attachment)
                # ----------------------------------------------------
                ticker = str(out.get("ticker") or "").strip().upper()
                market = None

                if ticker.startswith(("ASX:", "ASX_DLY:")):
                    market = "ASX"
                elif ticker.startswith(("LSE:", "LSE_DLY:")):
                    market = "LSE"
                elif ticker.startswith(("TSX:", "TSX_DLY:", "TSXV:", "TSXV_DLY:")):
                    market = "TSX"
                elif ticker.startswith((
                    "NYSE:", "NYSE_DLY:",
                    "NASDAQ:", "NASDAQ_DLY:"
                )):
                    market = "NYSE"

                out["_market"] = market

                # Attach market anchor if available
                if market and STATE["anchors"].get(market):
                    out["_anchor"] = copy.deepcopy(STATE["anchors"][market])

                # include server ts in this message too (helps comms/age)
                out["_server_ts"] = int(time.time() * 1000)

                # ----------------------------------------------------
                # ENFORCE MASTER GOVERNANCE FIELDS IN SCADA_STATUS
                # ----------------------------------------------------
                if typ == "SCADA_STATUS":
                    master_cycle_120 = STATE.get("cycle_120")
                    master_cycle     = STATE.get("cycle")

                    if master_cycle_120 is not None or master_cycle is not None:
                        out["cycle_120"] = master_cycle_120 if master_cycle_120 is not None else master_cycle
                        out["cycle"]     = master_cycle

                        if STATE.get("regime") is not None:
                            out["regime"] = STATE.get("regime")
                        if STATE.get("vol") is not None:
                            out["vol"] = STATE.get("vol")

                        if STATE.get("s1_allowed") is not None:
                            out["s1_allowed"] = STATE.get("s1_allowed")
                        if STATE.get("s2_allowed") is not None:
                            out["s2_allowed"] = STATE.get("s2_allowed")
                        if STATE.get("s3_watch") is not None:
                            out["s3_watch"] = STATE.get("s3_watch")
                        if STATE.get("s3_armed") is not None:
                            out["s3_armed"] = STATE.get("s3_armed")
                        if STATE.get("s3_allowed") is not None:
                            out["s3_allowed"] = STATE.get("s3_allowed")
                    else:
                        for k in (
                            "cycle_120", "cycle", "regime", "vol",
                            "s1_allowed", "s2_allowed", "s3_watch", "s3_armed", "s3_allowed"
                        ):
                            out.pop(k, None)

                    # ------------------------------------------------
                    # MESSAGE SYSTEM BRIDGE (derive from SCADA truth)
                    # ------------------------------------------------
                    def _truthy(v) -> bool:
                        if v is True:
                            return True
                        if v is False or v is None:
                            return False
                        s = str(v).strip().upper()
                        return s in ("1", "TRUE", "ON", "YES", "ACTIVE")

                    prev_msg = dict(STATE.get("message") or {})

                    setup_on = _truthy(out.get("setup_any")) or _truthy(out.get("pill_setup_any"))
                    trigger_on = (
                        _truthy(out.get("trigger_any")) or
                        _truthy(out.get("signal")) or
                        _truthy(out.get("signal_any"))
                    )

                    ticker = str(out.get("ticker") or "").strip().upper()
                    trig_text = "TRIGGER" if trigger_on else "—"

                    new_msg = {
                        "setup": bool(setup_on),
                        "trigger": trig_text,
                        "ticker": ticker,
                        "ref_id": ref_id,
                        "_ts": int(time.time() * 1000),
                    }

                    if new_msg != prev_msg:
                        STATE["message"] = new_msg
                        emit_event2 = "macro_update"
                        emit_payload2 = copy.deepcopy(STATE)

                # persist warm-start lanes
                if typ == "SCADA_STATUS":
                    STATE["stocks"]["last_scada_by_ref"][str(ref_id)] = out
                else:
                    STATE["stocks"]["last_watch_by_ref"][str(ref_id)] = out

                try:
                    _store_node_payload(out)
                except Exception:
                    pass

                _update_monitor_lane(_extract_meta(out))

                do_save = True
                emit_event = "stock_update"
                emit_payload = out

            else:
                # ------------------------------------------------
                # PINE AUTHORITY — MACRO + CARD4 (TRUTH)
                # Cards 1 & 3 MUST come from Pine MACRO payload
                # ------------------------------------------------
                pine_allow = {}
                for k in (
                    "macro_recession",
                    "s1_allowed",
                    "s2_allowed",
                    "s3_watch",
                    "s3_armed",
                    "s3_allowed",
                    "spx_cycle_high",
                    "spx_cycle_high_time",
                    "spx_high_frozen",
                    "spx_dd_pct",
                    "spx_dd35",
                    "cycle_120",
                    "mom",
                ):
                    if k in data:
                        pine_allow[k] = data.get(k)

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
                # MACRO V2 LANES
                # ------------------------------------------------
                if "macro_v2" not in STATE or not isinstance(STATE.get("macro_v2"), dict):
                    STATE["macro_v2"] = {
                        "gc": {
                            "state": None,
                            "trend_50sma": None,
                            "msa_pct": None,
                            "valid_signal": None,
                            "explain": None,
                        },
                        "gs": {
                            "state": None,
                            "trend_50sma": None,
                            "msa_pct": None,
                            "valid_signal": None,
                            "explain": None,
                        },
                        "walcl": {
                            "state": None,
                            "trend": None,
                            "roc": None,
                            "explain": None,
                        },
                        "fx": {
                            "gbpcad": {
                                "state": None,
                                "trend_50sma": None,
                                "msa_pct": None,
                            },
                            "gbpaud": {
                                "state": None,
                                "trend_50sma": None,
                                "msa_pct": None,
                            },
                            "context": None,
                        },
                        "internal": {
                            "state": None,
                            "explain": None,
                        },
                        "phase": {
                            "id": None,
                            "name": None,
                        },
                    }

                if typ == "MACRO_V2_RATIO":
                    lane = str(data.get("lane") or "").strip().lower()
                    if lane == "gold_copper":
                        STATE["macro_v2"]["gc"] = {
                            "state": _normalise_str(data.get("state")),
                            "trend_50sma": _normalise_str(data.get("trend_50sma")),
                            "msa_pct": _safe_float(data.get("msa_pct")),
                            "valid_signal": data.get("valid_signal"),
                            "explain": _normalise_str(data.get("explain")),
                            "latch_state": _normalise_str(data.get("gc_latch_state")) or "NEUTRAL",
                            "latch_count": _safe_int(data.get("gc_latch_count")) or 0,
                            "mean_duration": _safe_float(data.get("gc_mean_duration")) or 0.0,
                            "mean_mode": _normalise_str(data.get("gc_mean_mode")) or "MANUAL",
                            "green_mean": _safe_float(data.get("gc_green_mean_duration")) or 0.0,
                            "red_mean": _safe_float(data.get("gc_red_mean_duration")) or 0.0,
                            "orange_mean": _safe_float(data.get("gc_orange_mean_duration")) or 0.0,
                            "pct_of_mean": _safe_float(data.get("gc_pct")) or 0.0,
                            "phase": _normalise_str(data.get("gc_phase")) or "UNSET",
                            "support": bool(data.get("gc_support")),
                            "latch_changed": bool(data.get("gc_latch_changed")),
                        }

                    elif lane == "gold_silver":
                        STATE["macro_v2"]["gs"] = {
                            "state": _normalise_str(data.get("state")),
                            "trend_50sma": _normalise_str(data.get("trend_50sma")),
                            "msa_pct": _safe_float(data.get("msa_pct")),
                            "valid_signal": data.get("valid_signal"),
                            "explain": _normalise_str(data.get("explain")),
                        }

                elif typ == "MACRO_V2_LIQUIDITY":
                    if str(data.get("lane") or "").strip().lower() == "walcl":
                        STATE["macro_v2"]["walcl"] = {
                            "state": _normalise_str(data.get("walcl_state")),
                            "trend": _normalise_str(data.get("walcl_trend")),
                            "roc": _safe_float(data.get("walcl_roc")),
                            "explain": _normalise_str(data.get("walcl_explain")),
                        }

                elif typ == "MACRO_V2_FX":
                    if str(data.get("lane") or "").strip().lower() == "fx":
                        STATE["macro_v2"]["fx"]["gbpcad"] = {
                            "state": _normalise_str(data.get("gbpcad_state")),
                            "trend_50sma": _normalise_str(data.get("gbpcad_trend")),
                            "msa_pct": _safe_float(data.get("gbpcad_msa")),
                        }
                        STATE["macro_v2"]["fx"]["gbpaud"] = {
                            "state": _normalise_str(data.get("gbpaud_state")),
                            "trend_50sma": _normalise_str(data.get("gbpaud_trend")),
                            "msa_pct": _safe_float(data.get("gbpaud_msa")),
                        }

                # ------------------------------------------------
                # CARD 2 — CANONICAL (nested)
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

                # ------------------------------------------------
                # Recompute Macro V2 derived states
                # ------------------------------------------------
                _apply_macro_v2_normalisation()

                _recompute_war_from_secret()
                _update_monitor_lane(meta)
                print("MACRO_V2_STATE =", STATE.get("macro_v2"))

                payload = copy.deepcopy(STATE)

                do_save = True
                emit_event = "macro_update"
                emit_payload = payload

        # ====================================================
        # OUTSIDE LOCK: IO + EMITS (SAFE)
        # ====================================================
        if do_save:
            save_state_throttled()

        if emit_event and emit_payload is not None:
            socketio.emit(emit_event, _json_safe(emit_payload))

        if emit_event2 and emit_payload2 is not None:
            socketio.emit(emit_event2, _json_safe(emit_payload2))

        _log_debug("/webhook", data, ok=True)
        return "SUCCESS", 200

    except Exception as e:
        msg = str(e)
        _log_debug("/webhook", {"ok": False, "error": msg}, ok=False)
        return jsonify({"ok": False, "error": msg}), 400

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
@app.route("/sentinel_log_pretty")
@login_required
def sentinel_log_pretty():
    path = SENTINEL_LOG_FILE
    if not os.path.exists(path):
        path = SENTINEL_LOG_FALLBACK
    if not os.path.exists(path):
        return jsonify([])

    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except:
                pass

    return jsonify(rows[-100:])  # last 100 only


@app.route("/verify_secret", methods=["POST"])
def verify_secret():
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "unknown")
    ip = ip.split(",")[0].strip()

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
        snap = copy.deepcopy(STATE)

    # Full macro/state snapshot
    emit("macro_update", _json_safe(snap))

    # ----------------------------------------------------
    # Warm-start stock lanes so nodes repaint on refresh
    # ----------------------------------------------------
    stocks = snap.get("stocks", {}) if isinstance(snap.get("stocks"), dict) else {}

    for lane in stocks.get("last_scada_by_ref", {}).values():
        try:
            emit("stock_update", _json_safe(lane))
        except Exception:
            pass

    for lane in stocks.get("last_watch_by_ref", {}).values():
        try:
            emit("stock_update", _json_safe(lane))
        except Exception:
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    socketio.run(app, host="0.0.0.0", port=port)
