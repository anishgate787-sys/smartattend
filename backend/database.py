import sqlite3
import os

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "smartattend.db")
DB_PATH = os.environ.get("SMARTATTEND_DB_PATH", DEFAULT_DB_PATH)


def get_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('student', 'faculty')),
        device_id TEXT,
        roll_number TEXT,
        department TEXT,
        year TEXT,
        section TEXT,
        phone TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Add profile columns to databases created by older SmartAttend versions.
    existing_columns = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    for column in ("roll_number", "department", "year", "section", "phone"):
        if column not in existing_columns:
            cur.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        faculty_id INTEGER NOT NULL,
        FOREIGN KEY (faculty_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS enrollments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_id INTEGER NOT NULL,
        FOREIGN KEY (student_id) REFERENCES users(id),
        FOREIGN KEY (subject_id) REFERENCES subjects(id),
        UNIQUE(student_id, subject_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'ended')),
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT,
        FOREIGN KEY (subject_id) REFERENCES subjects(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        token_value TEXT UNIQUE NOT NULL,
        expires_at REAL NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        marked_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES sessions(id),
        FOREIGN KEY (student_id) REFERENCES users(id),
        UNIQUE(session_id, student_id)
    )
    """)

    conn.commit()
    conn.close()
