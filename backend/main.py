from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel
import os
import csv
import io
import qrcode


def load_local_env():
    """Load simple KEY=VALUE pairs for local development only."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

try:
    from .database import init_db, get_db
    from . import auth
    from . import attendance as attendance_logic
except ImportError:  # Supports `python main.py` from the backend directory.
    from database import init_db, get_db
    import auth
    import attendance as attendance_logic

APP_ENV = os.environ.get("SMARTATTEND_ENV", "development").lower()
if APP_ENV == "production":
    for name in ("SMARTATTEND_SECRET", "SMARTATTEND_TOKEN_SECRET"):
        if not os.environ.get(name):
            raise RuntimeError(f"{name} must be set when SMARTATTEND_ENV=production")

cors_origins = [origin.strip() for origin in os.environ.get(
    "SMARTATTEND_CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",") if origin.strip()]

app = FastAPI(title="SmartAttend API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


# ---------- Pydantic request models ----------

class SignupRequest(BaseModel):
    name: str
    email: str
    password: str
    role: str
    device_id: str = "unknown-device"
    roll_number: str = ""
    department: str = ""
    year: str = ""
    section: str = ""
    phone: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str
    device_id: str = "unknown-device"


class SubjectCreateRequest(BaseModel):
    name: str


class EnrollRequest(BaseModel):
    subject_id: int


class MarkAttendanceRequest(BaseModel):
    qr_payload: str


# ---------- Auth helper ----------

def require_user(request: Request):
    user = auth.get_current_user(request.headers.get("Authorization", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_role(request: Request, role: str):
    user = require_user(request)
    if user["role"] != role:
        raise HTTPException(status_code=403, detail=f"This action requires a {role} account")
    return user


# ---------- Auth routes ----------

@app.post("/auth/signup")
def signup_route(body: SignupRequest):
    result, error = auth.signup(body.name, body.email, body.password, body.role, body.device_id,
                                 body.roll_number, body.department, body.year, body.section, body.phone)
    if error:
        raise HTTPException(status_code=400, detail=error)
    return result


@app.post("/auth/login")
def login_route(body: LoginRequest):
    result, error = auth.login(body.email, body.password, body.device_id)
    if error:
        raise HTTPException(status_code=401, detail=error)
    return result


@app.get("/auth/me")
def me_route(request: Request):
    user = require_user(request)
    return {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}


class ProfileRequest(BaseModel):
    name: str
    roll_number: str = ""
    department: str = ""
    year: str = ""
    section: str = ""
    phone: str = ""


@app.get("/profile")
def profile(request: Request):
    user = require_user(request)
    return {key: user[key] or "" for key in ("id", "name", "email", "role", "roll_number", "department", "year", "section", "phone")}


@app.put("/profile")
def update_profile(body: ProfileRequest, request: Request):
    user = require_user(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    conn = get_db()
    conn.execute("""UPDATE users SET name = ?, roll_number = ?, department = ?, year = ?, section = ?, phone = ?
                   WHERE id = ?""", (name, body.roll_number.strip(), body.department.strip(), body.year.strip(),
                                     body.section.strip(), body.phone.strip(), user["id"]))
    conn.commit()
    conn.close()
    return {"status": "updated", "name": name}


# ---------- Subject routes ----------

@app.post("/subjects")
def create_subject(body: SubjectCreateRequest, request: Request):
    user = require_role(request, "faculty")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Subject name is required")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO subjects (name, faculty_id) VALUES (?, ?)", (name, user["id"]))
    conn.commit()
    subject_id = cur.lastrowid
    conn.close()
    return {"id": subject_id, "name": name}


@app.get("/subjects/mine")
def my_subjects(request: Request):
    user = require_user(request)
    conn = get_db()
    cur = conn.cursor()
    if user["role"] == "faculty":
        rows = cur.execute("SELECT * FROM subjects WHERE faculty_id = ?", (user["id"],)).fetchall()
    else:
        rows = cur.execute(
            """SELECT subjects.* FROM subjects
               JOIN enrollments ON subjects.id = enrollments.subject_id
               WHERE enrollments.student_id = ?""",
            (user["id"],),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/subjects/available")
def available_subjects(request: Request):
    """Subjects a student can enroll in (not already enrolled)."""
    user = require_role(request, "student")
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM subjects WHERE id NOT IN
           (SELECT subject_id FROM enrollments WHERE student_id = ?)""",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/subjects/enroll")
def enroll(body: EnrollRequest, request: Request):
    user = require_role(request, "student")
    conn = get_db()
    cur = conn.cursor()
    subject = cur.execute("SELECT * FROM subjects WHERE id = ?", (body.subject_id,)).fetchone()
    if not subject:
        conn.close()
        raise HTTPException(status_code=404, detail="Subject not found")
    try:
        cur.execute(
            "INSERT INTO enrollments (student_id, subject_id) VALUES (?, ?)",
            (user["id"], body.subject_id),
        )
        conn.commit()
    except Exception:
        conn.close()
        raise HTTPException(status_code=400, detail="Already enrolled")
    conn.close()
    return {"status": "enrolled", "subject_id": body.subject_id}


# ---------- Session / QR routes (faculty) ----------

class SessionStartRequest(BaseModel):
    subject_id: int


@app.post("/session/start")
def session_start(body: SessionStartRequest, request: Request):
    user = require_role(request, "faculty")
    session_id, error = attendance_logic.start_session(body.subject_id, user["id"])
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"session_id": session_id}


@app.get("/session/{session_id}/token")
def session_token(session_id: int, request: Request):
    user = require_role(request, "faculty")
    result, error = attendance_logic.generate_token(session_id, user["id"])
    if error:
        raise HTTPException(status_code=400, detail=error)
    return result


@app.post("/session/{session_id}/end")
def session_end(session_id: int, request: Request):
    user = require_role(request, "faculty")
    ok, error = attendance_logic.end_session(session_id, user["id"])
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"status": "ended"}


# ---------- Attendance routes (student) ----------

@app.post("/attendance/mark")
def mark_attendance_route(body: MarkAttendanceRequest, request: Request):
    user = require_role(request, "student")
    result, error = attendance_logic.mark_attendance(body.qr_payload, user["id"])
    if error:
        raise HTTPException(status_code=400, detail=error)
    return {"status": "marked", **result}


@app.get("/attendance/history")
def attendance_history(request: Request):
    user = require_role(request, "student")
    conn = get_db()
    rows = conn.execute(
        """SELECT attendance.marked_at, sessions.id as session_id, subjects.name as subject_name
           FROM attendance
           JOIN sessions ON attendance.session_id = sessions.id
           JOIN subjects ON sessions.subject_id = subjects.id
           WHERE attendance.student_id = ?
           ORDER BY attendance.marked_at DESC""",
        (user["id"],),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/attendance/summary")
def attendance_summary(request: Request):
    """Per-subject attendance percentage for the logged in student."""
    user = require_role(request, "student")
    conn = get_db()
    subjects = conn.execute(
        """SELECT subjects.* FROM subjects
           JOIN enrollments ON subjects.id = enrollments.subject_id
           WHERE enrollments.student_id = ?""",
        (user["id"],),
    ).fetchall()

    summary = []
    for subj in subjects:
        total_sessions = conn.execute(
            "SELECT COUNT(*) as c FROM sessions WHERE subject_id = ?", (subj["id"],)
        ).fetchone()["c"]
        attended = conn.execute(
            """SELECT COUNT(*) as c FROM attendance
               JOIN sessions ON attendance.session_id = sessions.id
               WHERE sessions.subject_id = ? AND attendance.student_id = ?""",
            (subj["id"], user["id"]),
        ).fetchone()["c"]
        pct = round((attended / total_sessions) * 100, 1) if total_sessions > 0 else 0.0
        summary.append({
            "subject_id": subj["id"],
            "subject_name": subj["name"],
            "total_sessions": total_sessions,
            "attended": attended,
            "percentage": pct,
        })
    conn.close()
    return summary


# ---------- Analytics routes (faculty) ----------

@app.get("/analytics/subject/{subject_id}")
def subject_analytics(subject_id: int, request: Request):
    user = require_role(request, "faculty")
    conn = get_db()
    subject = conn.execute(
        "SELECT * FROM subjects WHERE id = ? AND faculty_id = ?", (subject_id, user["id"])
    ).fetchone()
    if not subject:
        conn.close()
        raise HTTPException(status_code=404, detail="Subject not found")

    sessions = conn.execute(
        "SELECT * FROM sessions WHERE subject_id = ? ORDER BY started_at", (subject_id,)
    ).fetchall()
    session_stats = []
    for s in sessions:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM attendance WHERE session_id = ?", (s["id"],)
        ).fetchone()["c"]
        session_stats.append({"session_id": s["id"], "started_at": s["started_at"], "present_count": count})

    students = conn.execute(
        """SELECT users.id, users.name FROM users
           JOIN enrollments ON users.id = enrollments.student_id
           WHERE enrollments.subject_id = ?""",
        (subject_id,),
    ).fetchall()

    total_sessions = len(sessions)
    at_risk = []
    for stu in students:
        attended = conn.execute(
            """SELECT COUNT(*) as c FROM attendance
               JOIN sessions ON attendance.session_id = sessions.id
               WHERE sessions.subject_id = ? AND attendance.student_id = ?""",
            (subject_id, stu["id"]),
        ).fetchone()["c"]
        pct = round((attended / total_sessions) * 100, 1) if total_sessions > 0 else 0.0
        if pct < 75.0:
            at_risk.append({"student_id": stu["id"], "name": stu["name"], "percentage": pct})

    conn.close()
    return {
        "subject_name": subject["name"],
        "total_sessions": total_sessions,
        "enrolled_count": len(students),
        "attendance_rate": round(sum(item["present_count"] for item in session_stats) / (total_sessions * len(students)) * 100, 1) if total_sessions and students else 0.0,
        "sessions": session_stats,
        "at_risk_students": sorted(at_risk, key=lambda x: x["percentage"]),
    }


def _faculty_roster(subject_id: int, faculty_id: int):
    conn = get_db()
    subject = conn.execute("SELECT * FROM subjects WHERE id = ? AND faculty_id = ?", (subject_id, faculty_id)).fetchone()
    if not subject:
        conn.close()
        return None, None
    total = conn.execute("SELECT COUNT(*) AS c FROM sessions WHERE subject_id = ?", (subject_id,)).fetchone()["c"]
    rows = conn.execute("""SELECT u.id, u.name, u.email, u.roll_number, u.department, u.year, u.section,
                                COUNT(a.id) AS present_count
                         FROM users u
                         JOIN enrollments e ON e.student_id = u.id
                         LEFT JOIN attendance a ON a.student_id = u.id
                           AND a.session_id IN (SELECT id FROM sessions WHERE subject_id = ?)
                         WHERE e.subject_id = ?
                         GROUP BY u.id
                         ORDER BY u.name COLLATE NOCASE""", (subject_id, subject_id)).fetchall()
    conn.close()
    roster = []
    for row in rows:
        present = row["present_count"]
        roster.append({**dict(row), "present_count": present, "absent_count": max(total - present, 0),
                       "percentage": round(present / total * 100, 1) if total else 0.0,
                       "status": "Present" if total and present / total >= 0.75 else "Needs attention"})
    return subject, {"total_sessions": total, "students": roster}


@app.get("/analytics/subject/{subject_id}/roster")
def subject_roster(subject_id: int, request: Request):
    user = require_role(request, "faculty")
    subject, report = _faculty_roster(subject_id, user["id"])
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")
    return {"subject_name": subject["name"], **report}


@app.get("/analytics/subject/{subject_id}/export")
def export_subject(subject_id: int, request: Request):
    user = require_role(request, "faculty")
    subject, report = _faculty_roster(subject_id, user["id"])
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Student name", "Roll number", "Department", "Year", "Section", "Email", "Present", "Absent", "Attendance %", "Status"])
    for student in report["students"]:
        writer.writerow([student["name"], student["roll_number"] or "", student["department"] or "",
                         student["year"] or "", student["section"] or "", student["email"],
                         student["present_count"], student["absent_count"], student["percentage"], student["status"]])
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{subject["name"].replace(" ", "_")}_attendance.csv"'})


@app.get("/session/{session_id}/attendance")
def session_attendance(session_id: int, request: Request):
    user = require_role(request, "faculty")
    conn = get_db()
    session = conn.execute("""SELECT sessions.*, subjects.name AS subject_name FROM sessions
                              JOIN subjects ON subjects.id = sessions.subject_id
                              WHERE sessions.id = ? AND subjects.faculty_id = ?""", (session_id, user["id"])).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    rows = conn.execute("""SELECT u.name, u.roll_number, u.department,
                                 CASE WHEN a.id IS NULL THEN 0 ELSE 1 END AS present
                          FROM users u JOIN enrollments e ON e.student_id = u.id
                          LEFT JOIN attendance a ON a.student_id = u.id AND a.session_id = ?
                          WHERE e.subject_id = ? ORDER BY u.name COLLATE NOCASE""", (session_id, session["subject_id"])).fetchall()
    conn.close()
    return {"subject_name": session["subject_name"], "students": [{**dict(row), "status": "Present" if row["present"] else "Absent"} for row in rows]}


# ---------- Serve frontend static files ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/qr.png")
def qr_image(data: str):
    """Generate a high-contrast QR image without relying on a browser CDN."""
    if not data or len(data) > 2048:
        raise HTTPException(status_code=400, detail="Invalid QR data")
    image = qrcode.make(data, error_correction=qrcode.constants.ERROR_CORRECT_H, border=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(
        content=output.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )

if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
