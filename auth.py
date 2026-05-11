import os
import sqlite3
from functools import wraps
from flask import session, redirect, url_for, request, render_template
from werkzeug.security import generate_password_hash, check_password_hash


DB_PATH = os.getenv("AUTH_DB_PATH", "sutton_auth.db")
FOUNDER_USERNAME = os.getenv("FOUNDER_USERNAME", "founder")
FOUNDER_PASSWORD = os.getenv("FOUNDER_PASSWORD")


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'founder',
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                success INTEGER NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()


def seed_founder_user():
    if not FOUNDER_PASSWORD:
        print("AUTH WARNING: FOUNDER_PASSWORD not set. Founder user not seeded.")
        return

    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (FOUNDER_USERNAME,)
        ).fetchone()

        if existing:
            return

        conn.execute("""
            INSERT INTO users (username, password_hash, role, active)
            VALUES (?, ?, ?, 1)
        """, (
            FOUNDER_USERNAME,
            generate_password_hash(FOUNDER_PASSWORD),
            "founder"
        ))

        conn.commit()
        print(f"AUTH: founder user seeded: {FOUNDER_USERNAME}")


def get_user_by_username(username):
    with db_conn() as conn:
        return conn.execute("""
            SELECT *
            FROM users
            WHERE username = ?
              AND active = 1
        """, (username,)).fetchone()


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


def log_login_attempt(user, username, success):
    with db_conn() as conn:
        conn.execute("""
            INSERT INTO login_audit (
                user_id,
                username,
                success,
                ip_address,
                user_agent
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user["id"] if user else None,
            user["username"] if user else username,
            1 if success else 0,
            get_client_ip(),
            request.headers.get("User-Agent", "")
        ))

        conn.commit()


def user_login_count(user_id):
    with db_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS c
            FROM login_audit
            WHERE user_id = ?
              AND success = 1
        """, (user_id,)).fetchone()

        return row["c"] if row else 0


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def founder_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))

        if session.get("role") != "founder":
            return "Forbidden", 403

        return fn(*args, **kwargs)
    return wrapper


def register_auth_routes(app):

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")

            user = get_user_by_username(username)

            if user and check_password_hash(user["password_hash"], password):
                log_login_attempt(user, username, True)

                login_count = user_login_count(user["id"])

                session.clear()
                session["authenticated"] = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["login_count"] = login_count
                session["repeat_user"] = login_count > 1

                next_url = request.args.get("next") or url_for("terminal")
                return redirect(next_url)

            log_login_attempt(user, username, False)
            error = "Access denied."

        return render_template("login.html", error=error)


    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))


def init_auth(app):
    init_auth_db()
    seed_founder_user()
    register_auth_routes(app)
