import os
import tempfile
import unittest


class SmartAttendCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["SMARTATTEND_DB_PATH"] = os.path.join(cls.temp_dir.name, "test.db")
        os.environ["SMARTATTEND_SECRET"] = "test-login-secret"
        os.environ["SMARTATTEND_TOKEN_SECRET"] = "test-token-secret"
        from backend import database
        database.DB_PATH = os.environ["SMARTATTEND_DB_PATH"]
        database.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_password_hashes_are_salted_and_verifiable(self):
        from backend.auth import hash_password, verify_password
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_qr_token_can_be_used_by_multiple_enrolled_students(self):
        from backend import attendance, database
        conn = database.get_db()
        first = conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("Faculty", "faculty@test.local", "x", "faculty"),
        ).lastrowid
        student_one = conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("One", "one@test.local", "x", "student"),
        ).lastrowid
        student_two = conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("Two", "two@test.local", "x", "student"),
        ).lastrowid
        subject = conn.execute(
            "INSERT INTO subjects (name, faculty_id) VALUES (?, ?)", ("Math", first)
        ).lastrowid
        conn.execute("INSERT INTO enrollments (student_id, subject_id) VALUES (?, ?)", (student_one, subject))
        conn.execute("INSERT INTO enrollments (student_id, subject_id) VALUES (?, ?)", (student_two, subject))
        conn.commit()
        conn.close()

        session_id, error = attendance.start_session(subject, first)
        self.assertIsNone(error)
        token_data, error = attendance.generate_token(session_id, first)
        self.assertIsNone(error)
        _, error = attendance.mark_attendance(token_data["qr_payload"], student_one)
        self.assertIsNone(error)
        _, error = attendance.mark_attendance(token_data["qr_payload"], student_two)
        self.assertIsNone(error)
        parts = token_data["qr_payload"].split("|")
        self.assertEqual(len(parts), 4)
        self.assertGreater(float(parts[2]), __import__("time").time())

        from backend.main import _faculty_roster
        subject_row, report = _faculty_roster(subject, first)
        self.assertEqual(subject_row["name"], "Math")
        self.assertEqual(report["total_sessions"], 1)
        self.assertEqual([row["present_count"] for row in report["students"]], [1, 1])


if __name__ == "__main__":
    unittest.main()
