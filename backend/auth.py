import hashlib
import hmac
import os
import time
import base64
import json
try:
    from .database import get_db
except ImportError:  # Supports running modules directly from backend.
    from database import get_db

SECRET_KEY = os.environ.get("SMARTATTEND_SECRET", "dev-secret-change-this-in-production")
TOKEN_TTL_SECONDS = 60 * 60 * 12  # 12 hour login session
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS, _b64url_encode(salt), _b64url_encode(digest)
    )


def verify_password(password: str, password_hash: str) -> bool:
    # Keep existing local databases usable after upgrading to PBKDF2. A
    # successful legacy login can be re-hashed by an admin migration later.
    if "$" not in password_hash:
        legacy_salt = os.environ.get("SMARTATTEND_SALT", "smartattend-static-salt")
        legacy_digest = hashlib.sha256((legacy_salt + password).encode()).hexdigest()
        return hmac.compare_digest(legacy_digest, password_hash)
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), _b64url_decode(salt), int(iterations)
        )
        return hmac.compare_digest(_b64url_encode(digest), expected)
    except (ValueError, TypeError):
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = dict(payload)
    payload["exp"] = time.time() + TOKEN_TTL_SECONDS
    header_b64 = _b64url_encode(json.dumps(header).encode())
    payload_b64 = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    sig = hmac.new(SECRET_KEY.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def verify_jwt(token: str):
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        expected_sig = hmac.new(SECRET_KEY.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def signup(name: str, email: str, password: str, role: str, device_id: str,
           roll_number: str = "", department: str = "", year: str = "", section: str = "", phone: str = ""):
    if role not in ("student", "faculty"):
        return None, "Invalid role"
    if not name or not email or not password:
        return None, "Missing required fields"
    if len(password) < 6:
        return None, "Password must be at least 6 characters"

    conn = get_db()
    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    if existing:
        conn.close()
        return None, "An account with this email already exists"

    cur.execute(
        """INSERT INTO users
           (name, email, password_hash, role, device_id, roll_number, department, year, section, phone)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name.strip(), email.lower().strip(), hash_password(password), role, device_id,
         roll_number.strip(), department.strip(), year.strip(), section.strip(), phone.strip()),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()

    token = create_jwt({"user_id": user_id, "role": role, "device_id": device_id})
    return {"token": token, "user_id": user_id, "name": name.strip(), "role": role,
            "profile": {"roll_number": roll_number.strip(), "department": department.strip(),
                        "year": year.strip(), "section": section.strip(), "phone": phone.strip()}}, None


def login(email: str, password: str, device_id: str):
    conn = get_db()
    cur = conn.cursor()
    user = cur.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()

    if not user or not verify_password(password, user["password_hash"]):
        conn.close()
        return None, "Invalid email or password"

    # Device binding: if this account already has a bound device and a
    # different device is logging in, we don't hard-block (a student may
    # get a new phone) but we re-bind and this is the hook point where
    # you could add a "verify via email" step for stricter security.
    if user["device_id"] and user["device_id"] != device_id:
        cur.execute("UPDATE users SET device_id = ? WHERE id = ?", (device_id, user["id"]))
        conn.commit()
    elif not user["device_id"]:
        cur.execute("UPDATE users SET device_id = ? WHERE id = ?", (device_id, user["id"]))
        conn.commit()

    conn.close()
    token = create_jwt({"user_id": user["id"], "role": user["role"], "device_id": device_id})
    return {"token": token, "user_id": user["id"], "name": user["name"], "role": user["role"],
            "profile": {key: user[key] or "" for key in ("roll_number", "department", "year", "section", "phone")}}, None


def get_current_user(authorization_header: str):
    """authorization_header is the raw 'Bearer <token>' string."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[len("Bearer "):]
    payload = verify_jwt(token)
    if not payload:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (payload["user_id"],)).fetchone()
    conn.close()
    return user
