import sys
from auth import db_conn
from werkzeug.security import generate_password_hash

VALID_ROLES = ("founder", "operator", "observer")

def create_user(username, role, password):
    username = username.strip()
    role = role.strip().lower()

    if not username:
        print("ERROR: Username cannot be blank.")
        return 1

    if role not in VALID_ROLES:
        print(f"ERROR: Invalid role for {username}: {role}")
        return 1

    if not password:
        print(f"ERROR: Password cannot be blank for {username}.")
        return 1

    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if existing:
            print(f"SKIPPED: User already exists: {username}")
            return 0

        conn.execute("""
            INSERT INTO users (username, password_hash, role, active)
            VALUES (?, ?, ?, 1)
        """, (
            username,
            generate_password_hash(password),
            role
        ))

        conn.commit()

    print(f"CREATED: {username} [{role}]")
    return 0

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print('Usage: python create_user_batch.py "username" "role" "password"')
        sys.exit(1)

    sys.exit(create_user(sys.argv[1], sys.argv[2], sys.argv[3]))
