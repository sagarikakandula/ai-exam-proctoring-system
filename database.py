import os
import datetime
import sqlite3
import json
import time
from contextlib import contextmanager
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey,
    Text, Float, func
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, synonym

ROOT_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(ROOT_DIR, 'proctor.db')
SQLITE_URL = 'sqlite:///' + DB_PATH.replace('\\', '/')

# SQLAlchemy 2.0: do NOT use future=True on sessionmaker; bind engine directly
engine = create_engine(
    SQLITE_URL,
    connect_args={"check_same_thread": False},
    echo=False,          # set True temporarily to see every SQL statement
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def now():
    return datetime.datetime.utcnow()


class Student(Base):
    __tablename__ = 'students'

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    email = Column(String(256), default='')
    password = Column(String(256), nullable=False)
    year = Column(String(32), default='1st Year')
    branch = Column(String(64), default='')
    created_at = Column(DateTime, default=now)

    sessions = relationship('ExamSession', back_populates='student', cascade='all, delete-orphan')
    warnings = relationship('Warning', back_populates='student', cascade='all, delete-orphan')
    cheating_logs = relationship('CheatingLog', back_populates='student', cascade='all, delete-orphan')


class Admin(Base):
    __tablename__ = 'admins'

    id = Column(Integer, primary_key=True)
    admin_name = Column(String(128), unique=True, nullable=False)
    email = Column(String(256), default='')
    password = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=now)

    activity_logs = relationship('AdminActivityLog', back_populates='admin', cascade='all, delete-orphan')


class Exam(Base):
    __tablename__ = 'exams'

    id = Column(Integer, primary_key=True)
    exam_name = Column(String(256), nullable=False)
    subject = Column(String(256), default='')
    year_category = Column(String(32), default='1st Year')
    branch = Column(String(64), default='')
    created_by_admin_id = Column(Integer, ForeignKey('admins.id'), nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    duration_minutes = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)

    sessions = relationship('ExamSession', back_populates='exam', cascade='all, delete-orphan')


class ExamSession(Base):
    __tablename__ = 'exam_sessions'

    id = Column(String(64), primary_key=True)
    student_id = Column(Integer, ForeignKey('students.id'), nullable=True)
    exam_id = Column(Integer, ForeignKey('exams.id'), nullable=True)
    start_time = Column(DateTime, default=now)
    end_time = Column(DateTime, nullable=True)
    score = Column(Integer, default=0)
    total_questions = Column(Integer, default=0)
    integrity_score = Column(Float, default=100.0)
    status = Column(String(32), default='ongoing')
    created_at = Column(DateTime, default=now)

    student = relationship('Student', back_populates='sessions')
    exam = relationship('Exam', back_populates='sessions')
    warnings = relationship('Warning', back_populates='session', cascade='all, delete-orphan')
    cheating_logs = relationship('CheatingLog', back_populates='session', cascade='all, delete-orphan')


class CheatingLog(Base):
    __tablename__ = 'cheating_logs'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey('exam_sessions.id'), nullable=True)
    student_id = Column(Integer, ForeignKey('students.id'), nullable=True)
    cheating_type = Column(String(64), nullable=False)
    event_type = synonym('cheating_type')
    confidence_score = Column(Float, default=0.0)
    timestamp = Column(DateTime, default=now)
    photo_path = Column(String(512), default='')

    session = relationship('ExamSession', back_populates='cheating_logs')
    student = relationship('Student', back_populates='cheating_logs')


CheatingEvent = CheatingLog


class AdminActivityLog(Base):
    __tablename__ = 'admin_activity_logs'

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey('admins.id'), nullable=True)
    action = Column(String(128), nullable=False)
    description = Column(Text, default='')
    timestamp = Column(DateTime, default=now)

    admin = relationship('Admin', back_populates='activity_logs')


class Warning(Base):
    __tablename__ = 'warnings'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey('exam_sessions.id'), nullable=True)
    student_id = Column(Integer, ForeignKey('students.id'), nullable=True)
    warning_message = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=now)

    session = relationship('ExamSession', back_populates='warnings')
    student = relationship('Student', back_populates='warnings')


@contextmanager
def get_db():
    """Provide a transactional SQLAlchemy session.
    Each function that uses get_db() is responsible for calling db.commit().
    This manager only rolls back on unhandled exceptions and always closes.
    """
    db = SessionLocal()
    try:
        yield db
        # Do NOT commit here — each caller commits explicitly.
        # A missing commit inside the caller is a bug; we must not hide it.
    except Exception as exc:
        db.rollback()
        print(f"[DB][get_db] Rolled back due to: {exc}")
        raise
    finally:
        db.close()


def init_db():
    # ── Step 1: Fix schema of exam_sessions if it has the OLD layout ────────────
    # The old save_session() wrote: (id, student, started_at, ended_at, warnings, tab_switches, verdict)
    # SQLAlchemy expects:           (id, student_id, exam_id, start_time, end_time, score, status, ...)
    # If the table already exists with wrong columns, create_all() silently skips it.
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as _chk:
            # Try to SELECT a SQLAlchemy column. If it fails, schema is wrong.
            _chk.execute("SELECT student_id FROM exam_sessions LIMIT 1")
        print("[DB] exam_sessions schema OK")
    except sqlite3.OperationalError:
        # Wrong schema — rename old table so SQLAlchemy can recreate it
        try:
            with sqlite3.connect(DB_PATH, check_same_thread=False) as _fix:
                _fix.execute("ALTER TABLE exam_sessions RENAME TO exam_sessions_old")
                _fix.commit()
            print("[DB] ⚠️ Old exam_sessions schema detected — renamed to exam_sessions_old")
        except Exception as _e:
            print(f"[DB] Could not rename exam_sessions: {_e}")
    except Exception:
        pass  # Table probably doesn’t exist yet — create_all handles it

    # ── Step 2: Create / verify all SQLAlchemy tables ────────────────────────
    Base.metadata.create_all(engine)

    # ── Step 3: Legacy tables (questions / session_events / session_evidence) ──
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_checkpoint(FULL)")  # flush WAL → main db
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS questions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                type            TEXT    NOT NULL CHECK(type IN ('mcq','code','theory')),
                question        TEXT    NOT NULL,
                options         TEXT,
                correct_answer  INTEGER,
                code_prompt     TEXT,
                placeholder     TEXT,
                created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            );

            CREATE TABLE IF NOT EXISTS session_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT    NOT NULL,
                timestamp   INTEGER NOT NULL,
                msg         TEXT    NOT NULL,
                FOREIGN KEY (session_id) REFERENCES exam_sessions(id)
            );

            CREATE TABLE IF NOT EXISTS session_evidence (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT    NOT NULL,
                filename    TEXT    NOT NULL,
                msg         TEXT,
                timestamp   INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES exam_sessions(id)
            );
        """)
        try:
            conn.execute("ALTER TABLE cheating_logs ADD COLUMN confidence_score REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass

    # ── Step 4: Quick write test — verify DB is actually writable ───────────
    try:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as _t:
            _t.execute("CREATE TABLE IF NOT EXISTS _db_write_test (id INTEGER PRIMARY KEY, ts TEXT)")
            _t.execute("INSERT OR REPLACE INTO _db_write_test (id, ts) VALUES (1, datetime('now'))")
            _t.commit()
            row = _t.execute("SELECT ts FROM _db_write_test WHERE id=1").fetchone()
        print(f"[DB] ✅ Write test OK — DB is writable. Path: {DB_PATH}")
        if row:
            print(f"[DB]    Test timestamp: {row[0]}")
    except Exception as _we:
        print(f"[DB] ❌ WRITE TEST FAILED: {_we}  — DB may be read-only or locked!")

    print("[DB] Initialized →", DB_PATH)



def _get_sqlite_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def save_session(session_id, student, ended_at, started_at,
                 warnings, tab_switches, events, evidence):
    """Legacy JSON-compatible session saver (writes only to session_events / session_evidence).
    The main exam_sessions row is managed by SQLAlchemy (create_exam_session / finalize_exam_session).
    This function only appends event and evidence rows, avoiding column-name conflicts."""

    # Only insert into the plain SQLite helper tables (safe columns)
    with _get_sqlite_conn() as conn:
        conn.execute("DELETE FROM session_events WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM session_evidence WHERE session_id=?", (session_id,))

        for ev in events:
            conn.execute(
                "INSERT INTO session_events (session_id, timestamp, msg) VALUES (?,?,?)",
                (session_id, ev.get('timestamp', int(time.time())), ev.get('msg', ''))
            )
        for ev in evidence:
            conn.execute(
                """INSERT INTO session_evidence (session_id, filename, msg, timestamp)
                   VALUES (?,?,?,?)""",
                (session_id, ev.get('file', ''), ev.get('msg', ''), ev.get('timestamp', int(time.time())))
            )
    print(f"[DB] Legacy session events/evidence saved for session_id={session_id}")


def add_question(q_type, question, options=None, correct_answer=None,
                  code_prompt=None, placeholder=None):
    opts_json = json.dumps(options) if options else None
    with _get_sqlite_conn() as conn:
        cur = conn.execute(
            """INSERT INTO questions
               (type, question, options, correct_answer, code_prompt, placeholder)
               VALUES (?,?,?,?,?,?)""",
            (q_type, question, opts_json, correct_answer, code_prompt, placeholder)
        )
        return cur.lastrowid


def list_questions():
    with _get_sqlite_conn() as conn:
        rows = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get('options'):
            d['options'] = json.loads(d['options'])
        result.append(d)
    return result


def delete_question(q_id):
    with _get_sqlite_conn() as conn:
        cur = conn.execute("DELETE FROM questions WHERE id=?", (q_id,))
        return cur.rowcount > 0


def get_student_by_name(name):
    """Return a Student object that is safe to use AFTER the session closes."""
    if not name:
        return None
    with get_db() as db:
        student = db.query(Student).filter(Student.name == name).first()
        if student:
            db.expunge(student)   # detach cleanly so .id etc. are still accessible
        return student


def get_admin_by_name(admin_name):
    """Return an Admin object that is safe to use AFTER the session closes."""
    if not admin_name:
        return None
    with get_db() as db:
        admin = db.query(Admin).filter(Admin.admin_name == admin_name).first()
        if admin:
            db.expunge(admin)
        return admin


def create_or_update_student(name, password, email='', year='1st Year', branch=''):
    with get_db() as db:
        try:
            student = db.query(Student).filter(Student.name == name).first()
            if not student:
                student = Student(
                    name=name,
                    password=password,
                    email=email or '',
                    year=year or '1st Year',
                    branch=branch or '',
                )
                db.add(student)
                db.flush()            # assigns student.id before commit
                db.commit()
                db.refresh(student)
                print(f"[DB] ✅ Student saved to DB: {name} (id={student.id})")
            else:
                student.password = password
                if email:
                    student.email = email
                if year:
                    student.year = year
                if branch:
                    student.branch = branch
                db.commit()
                db.refresh(student)
                print(f"[DB] ✅ Student updated in DB: {name} (id={student.id})")
            db.expunge(student)
            return student
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ create_or_update_student FAILED for '{name}': {e}")
            raise


def create_or_update_admin(admin_name, password, email=''):
    with get_db() as db:
        try:
            admin = db.query(Admin).filter(Admin.admin_name == admin_name).first()
            if not admin:
                admin = Admin(admin_name=admin_name, password=password, email=email or '')
                db.add(admin)
                db.flush()
                db.commit()
                db.refresh(admin)
                print(f"[DB] ✅ Admin saved to DB: {admin_name} (id={admin.id})")
            else:
                admin.password = password
                if email:
                    admin.email = email
                db.commit()
                db.refresh(admin)
                print(f"[DB] ✅ Admin updated in DB: {admin_name} (id={admin.id})")
            db.expunge(admin)
            return admin
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ create_or_update_admin FAILED for '{admin_name}': {e}")
            raise


def create_exam_session(session_id, student_id=None, exam_id=None, start_time=None, status='ongoing'):
    with get_db() as db:
        try:
            session_obj = ExamSession(
                id=session_id,
                student_id=student_id,
                exam_id=exam_id,
                start_time=start_time or now(),
                status=status,
                score=0,
                total_questions=0,
                integrity_score=100.0,
            )
            db.add(session_obj)
            db.commit()
            db.refresh(session_obj)
            print(f"[DB] ✅ Exam session created: id={session_id} student_id={student_id}")
            db.expunge(session_obj)
            return session_obj
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ create_exam_session FAILED: {e}")
            raise


def finalize_exam_session(session_id, end_time=None, score=0,
                          total_questions=0, integrity_score=100.0,
                          status='completed'):
    with get_db() as db:
        try:
            session_obj = db.query(ExamSession).filter(ExamSession.id == session_id).first()
            if not session_obj:
                print(f"[DB] ⚠️ finalize_exam_session: session_id={session_id} NOT FOUND")
                return None
            session_obj.end_time = end_time or now()
            session_obj.score = score
            session_obj.total_questions = total_questions
            session_obj.integrity_score = integrity_score
            session_obj.status = status
            db.commit()
            print(f"[DB] ✅ Exam session finalized: id={session_id} status={status}")
            db.expunge(session_obj)
            return session_obj
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ finalize_exam_session FAILED: {e}")
            raise


def record_warning(session_id, student_id, warning_message, timestamp=None):
    if not session_id:
        return
    with get_db() as db:
        try:
            warning = Warning(
                session_id=session_id,
                student_id=student_id,
                warning_message=warning_message,
                timestamp=timestamp or now(),
            )
            db.add(warning)
            db.commit()
            print(f"[DB] ✅ Warning stored: session={session_id} student_id={student_id}")
            return warning
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ record_warning FAILED: {e}")
            raise


def record_cheating_log(session_id, student_id, cheating_type, timestamp=None, photo_path=''):
    if not session_id:
        print(f"[DB] ⚠️ record_cheating_log skipped — no session_id")
        return
    with get_db() as db:
        try:
            log = CheatingLog(
                session_id=session_id,
                student_id=student_id,
                cheating_type=cheating_type,
                timestamp=timestamp or now(),
                photo_path=photo_path or '',
            )
            db.add(log)
            db.commit()
            print(f"[DB] ✅ Cheating event stored: session_id={session_id} student_id={student_id} type={cheating_type}")
            return log
        except Exception as e:
            db.rollback()
            print(f"[DB] ❌ record_cheating_log FAILED: {e}")
            raise


def create_admin_activity(admin_id, action, description=''):
    with get_db() as db:
        activity = AdminActivityLog(
            admin_id=admin_id,
            action=action,
            description=description,
            timestamp=now(),
        )
        db.add(activity)
        db.commit()
        return activity


# ─────────────────────────────────────────────────────────────────────────────
# COUNT FUNCTIONS — use raw sqlite3 (100% reliable, independent of SQLAlchemy
# session state or schema mismatches)
# ─────────────────────────────────────────────────────────────────────────────

def _raw_count(sql, params=()):
    """Execute a COUNT query via raw sqlite3 and return the integer result."""
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"[DB] _raw_count error ({sql[:50]}): {e}")
        return 0


def count_total_students():
    return _raw_count("SELECT COUNT(*) FROM students")


def count_total_admins():
    return _raw_count("SELECT COUNT(*) FROM admins")


def count_total_exams():
    return _raw_count("SELECT COUNT(*) FROM exams")


def count_cheating_events():
    return _raw_count("SELECT COUNT(*) FROM cheating_logs")


def count_warnings():
    return _raw_count("SELECT COUNT(*) FROM warnings")


def count_active_sessions():
    return _raw_count("SELECT COUNT(*) FROM exam_sessions WHERE status='ongoing'")


def count_completed_sessions():
    return _raw_count(
        "SELECT COUNT(*) FROM exam_sessions WHERE status IN ('completed','terminated')"
    )


def count_not_attended_students():
    return _raw_count("""
        SELECT COUNT(*) FROM students
        WHERE id NOT IN (
            SELECT DISTINCT student_id FROM exam_sessions
            WHERE student_id IS NOT NULL
        )
    """)


def avg_integrity_score():
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        row = conn.execute("SELECT AVG(integrity_score) FROM exam_sessions").fetchone()
        conn.close()
        return float(row[0] or 0.0) if row else 0.0
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# DIRECT SYNC — write users.json accounts to DB using raw sqlite3
# Called at startup so existing accounts are always visible in the dashboard
# ─────────────────────────────────────────────────────────────────────────────

def raw_sync_users(users_dict):
    """Write every user from users_dict into the DB via raw sqlite3.
    Uses INSERT OR IGNORE so existing rows are never overwritten.
    This is the most reliable path — no SQLAlchemy session involved.
    """
    now_str = datetime.datetime.utcnow().isoformat()
    admins_done = students_done = 0
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        for username, data in users_dict.items():
            role     = data.get('role', 'student')
            password = data.get('password', '')
            try:
                if role == 'admin':
                    conn.execute(
                        "INSERT OR IGNORE INTO admins (admin_name, password, email, created_at) "
                        "VALUES (?, ?, '', ?)",
                        (username, password, now_str),
                    )
                    admins_done += 1
                    print(f"[DB] ✅ Admin synced to DB: {username}")
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO students (name, password, email, year, branch, created_at) "
                        "VALUES (?, ?, '', '1st Year', '', ?)",
                        (username, password, now_str),
                    )
                    students_done += 1
                    print(f"[DB] ✅ Student synced to DB: {username}")
            except Exception as e:
                print(f"[DB] ❌ raw_sync_users failed for '{username}': {e}")
        conn.commit()
        conn.close()
        print(f"[DB] ✅ raw_sync_users complete: {admins_done} admin(s), {students_done} student(s)")
        print(f"[DB]    Current counts — students: {count_total_students()}, admins: {count_total_admins()}")
    except Exception as e:
        print(f"[DB] ❌ raw_sync_users CRITICAL FAILURE: {e}")


def list_admin_activity_logs(limit=20):
    with get_db() as db:
        logs = db.query(AdminActivityLog).order_by(AdminActivityLog.timestamp.desc()).limit(limit).all()
        return [
            {
                'id': item.id,
                'admin_name': item.admin.admin_name if item.admin else 'Unknown',
                'action': item.action,
                'description': item.description,
                'timestamp': int(item.timestamp.timestamp()),
            }
            for item in logs
        ]


def list_exam_sessions():
    with get_db() as db:
        sessions = db.query(ExamSession).order_by(ExamSession.start_time.desc()).all()
        result = []
        for item in sessions:
            student = item.student
            exam = item.exam
            warnings_count = db.query(Warning).filter(Warning.session_id == item.id).count()
            result.append({
                'id': item.id,
                'student': student.name if student else 'Unknown',
                'year': student.year if student else 'Unknown',
                'branch': student.branch if student else '',
                'exam_name': exam.exam_name if exam else 'Default Exam',
                'score': item.score,
                'integrity_score': item.integrity_score,
                'status': item.status,
                'started_at': int(item.start_time.timestamp()) if item.start_time else 0,
                'ended_at': int(item.end_time.timestamp()) if item.end_time else 0,
                'warnings': warnings_count,
            })
        return result


def get_exam_session_details(session_id):
    with get_db() as db:
        item = db.query(ExamSession).filter(ExamSession.id == session_id).first()
        if not item:
            return None
        student = item.student
        exam = item.exam
        warnings_list = db.query(Warning).filter(Warning.session_id == session_id).order_by(Warning.timestamp).all()
        cheating = db.query(CheatingLog).filter(CheatingLog.session_id == session_id).order_by(CheatingLog.timestamp).all()

        return {
            'id': item.id,
            'student': student.name if student else 'Unknown',
            'year': student.year if student else 'Unknown',
            'branch': student.branch if student else '',
            'exam_name': exam.exam_name if exam else 'Default Exam',
            'score': item.score,
            'integrity_score': item.integrity_score,
            'status': item.status,
            'started_at': int(item.start_time.timestamp()) if item.start_time else 0,
            'ended_at': int(item.end_time.timestamp()) if item.end_time else 0,
            'warnings': [
                {'message': w.warning_message, 'timestamp': int(w.timestamp.timestamp())}
                for w in warnings_list
            ],
            'cheating_logs': [
                {'type': c.cheating_type, 'timestamp': int(c.timestamp.timestamp()), 'photo_path': c.photo_path}
                for c in cheating
            ],
        }


def delete_all_exam_sessions():
    with get_db() as db:
        db.query(Warning).delete()
        db.query(CheatingLog).delete()
        deleted = db.query(ExamSession).delete()
        db.commit()
        return deleted


def delete_exam_session(session_id):
    with get_db() as db:
        db.query(Warning).filter(Warning.session_id == session_id).delete()
        db.query(CheatingLog).filter(CheatingLog.session_id == session_id).delete()
        deleted = db.query(ExamSession).filter(ExamSession.id == session_id).delete()
        db.commit()
        return deleted > 0


def list_student_sessions(student_id):
    with get_db() as db:
        sessions = db.query(ExamSession).filter(ExamSession.student_id == student_id).order_by(ExamSession.start_time.desc()).all()
        result = []
        for item in sessions:
            warnings_count = db.query(Warning).filter(Warning.session_id == item.id).count()
            cheating_count = db.query(CheatingLog).filter(CheatingLog.session_id == item.id).count()
            result.append({
                'id': item.id,
                'exam_name': item.exam.exam_name if item.exam else 'Default Exam',
                'score': item.score,
                'integrity_score': item.integrity_score,
                'status': item.status,
                'warnings': warnings_count,
                'cheating_flags': cheating_count,
                'started_at': int(item.start_time.timestamp()) if item.start_time else 0,
                'ended_at': int(item.end_time.timestamp()) if item.end_time else 0,
            })
        return result


def delete_student_by_name(name):
    with get_db() as db:
        student = db.query(Student).filter(Student.name == name).first()
        if not student:
            return False
        db.delete(student)
        db.commit()
        return True
