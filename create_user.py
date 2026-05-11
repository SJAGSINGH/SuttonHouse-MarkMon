import getpass
from auth import db_conn
from werkzeug.security import generate_password_hash


def create_user():
    username = input("Username: ").strip()
    role = input("Role founder/operator/observer: ").strip()
    password = getpass.getpass("Password: ")

    if role not in ("founder", "operator", "observer"):
        print("Invalid role.")
        return

    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if existing:
            print("User already exists.")
            return

        conn.execute("""
            INSERT INTO users (username, password_hash, role, active)
            VALUES (?, ?, ?, 1)
        """, (
            username,
            generate_password_hash(password),
            role
        ))

        conn.commit()

    print(f"Created user: {username} ({role})")


if __name__ == "__main__":
    create_user()
