import hashlib
import hmac
import os
import time
import secrets
try:
    from .database import get_db
except ImportError:  # Supports running modules directly from backend.
    from database import get_db

TOKEN_SECRET = os.environ.get("SMARTATTEND_TOKEN_SECRET", "dev-token-secret-change-this")
TOKEN_LIFETIME_SECONDS = 15  # Short-lived, but practical for a phone camera scan.

# very small in-memory rate limiter: student_id -> list of recent attempt timestamps
_scan_attempts = {}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_ATTEMPTS = 8


def _rate_limited(student_id: int) -> bool:
    now = time.time()
    attempts = _scan_attempts.get(student_id, [])
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    attempts.append(now)
    _scan_attempts[student_id] = attempts
    return len(attempts) > RATE_LIMIT_MAX_ATTEMPTS


def start_session(subject_id: int, faculty_id: int):
    conn = get_db()
    cur = conn.cursor()

    subject = cur.execute(
        "SELECT * FROM subjects WHERE id = ? AND faculty_id = ?", (subject_id, faculty_id)
    ).fetchone()
    if not subject:
        conn.close()
        return None, "Subject not found or not owned by this faculty account"

    # Auto-close any previous active session for this subject so a faculty
    # accidentally starting two sessions can't create ambiguous state.
    cur.execute(
        "UPDATE sessions SET status = 'ended', ended_at = CURRENT_TIMESTAMP "
        "WHERE subject_id = ? AND status = 'active'",
        (subject_id,),
    )

    cur.execute(
        "INSERT INTO sessions (subject_id, status) VALUES (?, 'active')", (subject_id,)
    )
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return session_id, None


def _sign(session_id: int, nonce: str, expires_at: float) -> str:
    msg = f"{session_id}:{nonce}:{expires_at}".encode()
    return hmac.new(TOKEN_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def generate_token(session_id: int, faculty_id: int):
    conn = get_db()
    cur = conn.cursor()
    session = cur.execute(
        """SELECT sessions.* FROM sessions
           JOIN subjects ON sessions.subject_id = subjects.id
           WHERE sessions.id = ? AND subjects.faculty_id = ?""",
        (session_id, faculty_id),
    ).fetchone()

    if not session:
        conn.close()
        return None, "Session not found or not owned by this faculty account"
    if session["status"] != "active":
        conn.close()
        return None, "Session has ended"

    nonce = secrets.token_urlsafe(16)
    expires_at = time.time() + TOKEN_LIFETIME_SECONDS
    sig = _sign(session_id, nonce, expires_at)
    # Use "|" as the delimiter, not ".", because expires_at is a float and
    # contains a decimal point — splitting on "." would break the payload apart.
    token_value = f"{session_id}|{nonce}|{expires_at}|{sig}"

    cur.execute(
        "INSERT INTO tokens (session_id, token_value, expires_at, used) VALUES (?, ?, ?, 0)",
        (session_id, token_value, expires_at),
    )
    conn.commit()

    present_count = cur.execute(
        "SELECT COUNT(*) as c FROM attendance WHERE session_id = ?", (session_id,)
    ).fetchone()["c"]
    enrolled_count = cur.execute(
        "SELECT COUNT(*) as c FROM enrollments WHERE subject_id = ?", (session["subject_id"],)
    ).fetchone()["c"]

    conn.close()
    return {
        "qr_payload": token_value,
        "expires_in": TOKEN_LIFETIME_SECONDS,
        "marked_count": present_count,
        "enrolled_count": enrolled_count,
    }, None


def end_session(session_id: int, faculty_id: int):
    conn = get_db()
    cur = conn.cursor()
    session = cur.execute(
        """SELECT sessions.* FROM sessions
           JOIN subjects ON sessions.subject_id = subjects.id
           WHERE sessions.id = ? AND subjects.faculty_id = ?""",
        (session_id, faculty_id),
    ).fetchone()
    if not session:
        conn.close()
        return False, "Session not found"
    cur.execute(
        "UPDATE sessions SET status = 'ended', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )
    conn.commit()
    conn.close()
    return True, None


def mark_attendance(qr_payload: str, student_id: int):
    if _rate_limited(student_id):
        return None, "Too many scan attempts. Please wait a moment and try again."

    try:
        session_id_str, nonce, expires_at_str, sig = qr_payload.split("|")
        session_id = int(session_id_str)
        expires_at = float(expires_at_str)
    except (ValueError, AttributeError):
        return None, "Invalid QR code"

    expected_sig = _sign(session_id, nonce, expires_at)
    if not hmac.compare_digest(expected_sig, sig):
        # Signature mismatch means the QR was tampered with / forged.
        return None, "Invalid or tampered QR code"

    if time.time() > expires_at:
        return None, "QR code expired — ask faculty for the current code"

    conn = get_db()
    cur = conn.cursor()

    token_row = cur.execute(
        "SELECT * FROM tokens WHERE token_value = ?", (qr_payload,)
    ).fetchone()
    if not token_row:
        conn.close()
        return None, "QR code not recognized"
    if time.time() > token_row["expires_at"]:
        conn.close()
        return None, "QR code expired - ask faculty for the current code"

    session = cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session or session["status"] != "active":
        conn.close()
        return None, "This session has ended"

    enrolled = cur.execute(
        "SELECT * FROM enrollments WHERE student_id = ? AND subject_id = ?",
        (student_id, session["subject_id"]),
    ).fetchone()
    if not enrolled:
        conn.close()
        return None, "You are not enrolled in this subject"

    already_marked = cur.execute(
        "SELECT * FROM attendance WHERE session_id = ? AND student_id = ?",
        (session_id, student_id),
    ).fetchone()
    if already_marked:
        conn.close()
        return None, "Attendance already marked for this session"

    # A QR token is intentionally reusable until it expires: every enrolled
    # student in the active session must be able to scan the same code.
    # The attendance UNIQUE constraint prevents duplicate marks per student.
    try:
        cur.execute(
            "INSERT INTO attendance (session_id, student_id) VALUES (?, ?)",
            (session_id, student_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        return None, "Could not mark attendance — it may already be recorded"

    conn.close()
    return {"session_id": session_id}, None
