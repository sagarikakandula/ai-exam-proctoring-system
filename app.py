from flask import Flask, render_template, Response, jsonify, request, send_from_directory, redirect, url_for, session
import os
import cv2
import numpy as np
import threading
import time
import json
import uuid
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import database
legacy_db = database

from modules.vision import VisionMonitor
from modules.audio import AudioMonitor
from modules.os_monitor import OSMonitor
try:
    from modules.face_recog import FaceRecognizer
    face_recognizer = FaceRecognizer()
    print("[App] Face recognition ready.")
except Exception as _e:
    face_recognizer = None
    print(f"[App] Face recognition unavailable: {_e}")

app = Flask(__name__)
app.secret_key = "AI_PROCTOR_SECRET_KEY"  # for sessions

# Initialise SQLite DB
database.init_db()

# ── Sync users.json → SQLAlchemy DB on every startup ─────────────────────────
# users.json is the legacy auth store; the SQLAlchemy DB is the source of truth
# for the admin dashboard. This function bridges the gap on each boot.
def _sync_users_to_db():
    """Read users.json and write every account to DB via raw sqlite3 (guaranteed)."""
    try:
        if not os.path.exists(USER_FILE):
            print("[Sync] users.json not found - skipping sync")
            return
        with open(USER_FILE, "r") as f:
            users = json.load(f)
    except Exception as e:
        print(f"[Sync] Could not read users.json: {e}")
        return
    # Use raw sqlite3 path - bypasses all SQLAlchemy session/schema issues
    database.raw_sync_users(users)


_sync_users_to_db()   # Run immediately at startup

# ── Global state ──────────────────────────────────────────────────────────────
cheat_stats = {
    "warnings":           0,
    "face_mismatch_count": 0,    # dedicated face-mismatch strike counter (max 3)
    "tab_switches":        0,
    "events":              [],
    "evidence":            [],
}
is_exam_active   = False
AUTO_STOP_LIMIT  = 25   # auto-stop after this many distinct warnings
TAB_SWITCH_LIMIT = 3    # terminate after this many tab-switch violations

try:
    vision_monitor = VisionMonitor()
    print("[App] Vision monitor ready.")
except Exception as _e:
    vision_monitor = None
    print(f"[App] Vision monitor unavailable: {_e}")

try:
    from modules.audio import AudioMonitor
    audio_monitor = AudioMonitor()
    print("[App] Audio monitor ready.")
except Exception as _e:
    audio_monitor = None
    print(f"[App] Audio monitor unavailable: {_e}")

try:
    from modules.os_monitor import OSMonitor
    os_monitor = OSMonitor()
    print("[App] OS monitor ready.")
except Exception as _e:
    os_monitor = None
    print(f"[App] OS monitor unavailable: {_e}")

# LIVE STREAMS: map username -> raw_frame_bytes
ACTIVE_STREAMS = {}
PROCESSED_STREAMS = {}

EVIDENCE_DIR  = os.path.join('static', 'evidence')
SESSIONS_DIR  = os.path.join('static', 'sessions')
os.makedirs(EVIDENCE_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── Auth Helpers ──────────────────────────────────────────────────────────────
USER_FILE = "users.json"

def load_users():
    if os.path.exists(USER_FILE):
        try:
            with open(USER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_users(users):
    with open(USER_FILE, "w") as f:
        json.dump(users, f, indent=4)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or session.get('role') != 'admin':
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session or session.get('role') != 'student':
            return redirect(url_for('student_login'))
        return f(*args, **kwargs)
    return decorated_function


def log_admin_action(action, description=''):
    admin_username = session.get('username') if session.get('role') == 'admin' else None
    if not admin_username:
        return
    admin = database.get_admin_by_name(admin_username)
    if not admin:
        return
    database.create_admin_activity(admin.id, action, description)


def infer_cheating_type(msg):
    lower = (msg or '').lower()
    if 'tab' in lower or 'window' in lower or 'visibility' in lower:
        return 'tab_switch'
    if 'multiple people' in lower or 'more than one' in lower or 'multiple faces' in lower:
        return 'multiple_faces'
    if 'face mismatch' in lower or 'face missing' in lower:
        return 'face_missing'
    if 'looking' in lower or 'gaze' in lower or 'eye' in lower:
        return 'eye_gaze'
    return 'other'

# ── Logging ───────────────────────────────────────────────────────────────────
# Per-type cooldown prevents the same infraction type from flooding within N seconds
_WARN_COOLDOWNS: dict = {}
WARN_TYPE_COOLDOWN = 30.0  # seconds between same-type warnings (was 8s)

def _warn_type_key(msg: str) -> str:
    """Extract a short type key from a message for cooldown bucketing."""
    m = msg.lower()
    for kw in ("multiple people", "face mismatch", "no person", "looking",
               "phone", "book", "audio", "window switched", "copy-paste",
               "macro", "tab", "shortcut", "unusual typing"):
        if kw in m:
            return kw
    return msg[:30]   # fallback: first 30 chars

def log_infraction(msg, evidence_file=None):
    global cheat_stats, is_exam_active

    # Per-type deduplication — suppress if same category was logged recently
    key = _warn_type_key(msg)
    now = time.time()
    if now - _WARN_COOLDOWNS.get(key, 0) < WARN_TYPE_COOLDOWN:
        # Update evidence silently even if warn is suppressed
        if evidence_file:
            cheat_stats["evidence"].append({
                "file": evidence_file, "msg": msg, "timestamp": int(now),
            })
        return
    _WARN_COOLDOWNS[key] = now

    # ── Face-mismatch: dedicated 3-strike system ────────────────────────────────
    is_face_mismatch = "FACE_MISMATCH:" in msg
    if is_face_mismatch:
        cheat_stats["face_mismatch_count"] += 1
        strike = cheat_stats["face_mismatch_count"]
        print(f"[!] IDENTITY STRIKE {strike}/3: {msg}")
        if strike >= 3 and is_exam_active:
            print("[!] IDENTITY STRIKE 3 — terminating exam.")
            # Log all 3 strikes then stop
            event = {"timestamp": int(now), "msg": msg, "face_mismatch_strike": strike}
            cheat_stats["events"].append(event)
            cheat_stats["warnings"] += 1
            if evidence_file:
                cheat_stats["evidence"].append({
                    "file": evidence_file, "msg": msg, "timestamp": int(now),
                })
            stop_exam()
            return

    print(f"[!] INCIDENT: {msg}")
    event = {"timestamp": int(now), "msg": msg}
    cheat_stats["events"].append(event)
    cheat_stats["warnings"] += 1

    session_id = cheat_stats.get("session_id")
    student = database.get_student_by_name(cheat_stats.get("student", ""))
    student_id = student.id if student else None
    try:
        database.record_warning(session_id, student_id, msg, timestamp=datetime.utcfromtimestamp(int(now)))
        database.record_cheating_log(
            session_id=session_id,
            student_id=student_id,
            cheating_type=infer_cheating_type(msg),
            timestamp=datetime.utcfromtimestamp(int(now)),
            photo_path=evidence_file or ''
        )
    except Exception as e:
        print(f"[DB] Infraction save error: {e}")

    if evidence_file:
        cheat_stats["evidence"].append({
            "file": evidence_file,
            "msg":  msg,
            "timestamp": int(now),
        })

    # Keep last 50 events
    if len(cheat_stats["events"]) > 50:
        cheat_stats["events"] = cheat_stats["events"][-50:]

    # Auto-stop if threshold hit
    if cheat_stats["warnings"] >= AUTO_STOP_LIMIT and is_exam_active:
        print(f"[!] AUTO-STOP: {AUTO_STOP_LIMIT} warnings reached.")
        stop_exam()

def save_session():
    """Persist completed exam stats to a timestamped JSON file AND SQLAlchemy DB."""
    ts    = cheat_stats.get("session_id") or uuid.uuid4().hex
    fname = f"session_{ts}.json"
    path  = os.path.join(SESSIONS_DIR, fname)
    ended = int(time.time())
    started = cheat_stats.get("started_at", ended)
    data  = {
        "id":          ts,
        "filename":    fname,
        "started_at": started,
        "ended_at":    ended,
        "student":     cheat_stats.get("student", "Unknown"),
        "warnings":    cheat_stats["warnings"],
        "tab_switches": cheat_stats.get("tab_switches", 0),
        "events":      cheat_stats["events"],
        "evidence":    cheat_stats["evidence"],
    }
    # Save JSON (backwards-compatible)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

    try:
        legacy_db.save_session(
            session_id=ts,
            student=data["student"],
            ended_at=ended,
            started_at=started,
            warnings=data["warnings"],
            tab_switches=data["tab_switches"],
            events=data["events"],
            evidence=data["evidence"],
        )
    except Exception as e:
        print(f"[DB] Legacy session save error: {e}")

    try:
        student = database.get_student_by_name(data["student"])
        student_id = student.id if student else None
        score = max(0, 100 - data["warnings"] * 5 - data["tab_switches"] * 3)
        integrity_score = max(0.0, 100.0 - data["warnings"] * 4 - data["tab_switches"] * 2)
        status = 'terminated' if data["warnings"] >= AUTO_STOP_LIMIT or data["tab_switches"] >= TAB_SWITCH_LIMIT else 'completed'
        database.finalize_exam_session(
            session_id=ts,
            end_time=datetime.utcfromtimestamp(ended),
            score=score,
            total_questions=0,
            integrity_score=integrity_score,
            status=status,
        )
    except Exception as e:
        print(f"[DB] Session finalize error: {e}")

    print(f"[Session] Saved -> {fname}")
    return fname

def stop_exam():
    global is_exam_active, audio_monitor, os_monitor
    if is_exam_active:
        save_session()
    is_exam_active = False
    if audio_monitor:
        audio_monitor.stop()
        audio_monitor = None
    if os_monitor:
        os_monitor.stop()
        os_monitor = None

# ── Video feed ────────────────────────────────────────────────────────────────
def generate_frames(username=None):
    """Serve processed frames for a specific student."""
    while True:
        if username and username in PROCESSED_STREAMS:
            frame_bytes = PROCESSED_STREAMS[username]
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        else:
            # Placeholder if no stream
            black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(black_frame, "No Active Feed", (180, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            ret, buffer = cv2.imencode('.jpg', black_frame)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(0.3) # Avoid tight loop

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', title="ScoreHunt | Home")

@app.route('/check')
@student_required
def check():
    return render_template('check.html')

@app.route('/exam')
@student_required
def exam():
    return render_template('exam.html')

@app.route('/results')
@student_required
def results():
    return render_template('results.html')

@app.route('/student/verify_id')
@student_required
def verify_id():
    return render_template('verify_id.html')

@app.route('/student/verify_face')
@student_required
def verify_face():
    return render_template('verify_face.html')

@app.route('/student')
@student_required
def student():
    return render_template('student.html')

@app.route('/student/register', methods=['GET', 'POST'])
def student_register():
    """Register a new student account."""
    users = load_users()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email', '').strip()
        year = request.form.get('year', '1st Year').strip() or '1st Year'
        branch = request.form.get('branch', '').strip()
        if username in users:
            return render_template('student_signup.html', error="User already exists")
        users[username] = {
            "password": generate_password_hash(password),
            "role": "student"
        }
        save_users(users)
        database.create_or_update_student(username, users[username]['password'], email=email, year=year, branch=branch)
        return redirect(url_for('student_login'))
    return render_template('student_signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Unified login flow for students and admins."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role     = request.form.get('role', 'student') # Default to student
        
        users = load_users()
        user_data = users.get(username)
        if not user_data:
            if role == 'student':
                user_obj = database.get_student_by_name(username)
            else:
                user_obj = database.get_admin_by_name(username)
            if user_obj:
                user_data = {'password': user_obj.password, 'role': role}

        if user_data and user_data.get('role') == role and check_password_hash(user_data['password'], password):
            session['logged_in'] = True
            session['username']  = username
            session['role']      = role
            
            if role == 'student':
                database.create_or_update_student(username, user_data['password'])
                return redirect(url_for('verify_id'))
            database.create_or_update_admin(username, user_data['password'])
            return redirect(url_for('admin'))
                
        return render_template('login.html', error=f"Invalid {role} credentials")
        
    return render_template('login.html')

@app.route('/admin/register', methods=['GET', 'POST'])
@app.route('/admin/signup', methods=['GET', 'POST'])
def admin_register():
    """Register the first/new admin account."""
    users = load_users()
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        email = request.form.get('email', '').strip()
        secret_key = request.form.get('secret_key', '').strip()
        if secret_key != 'SCOREHUNT_ADMIN_2024':
            return render_template('admin_signup.html', error='Invalid secret key')
        if username in users:
            return render_template('admin_signup.html', error="User already exists")
        users[username] = {
            "password": generate_password_hash(password),
            "role": "admin"
        }
        save_users(users)
        database.create_or_update_admin(username, users[username]['password'], email=email)
        return redirect(url_for('admin_login'))
    return render_template('admin_signup.html')

@app.route('/student/login', methods=['GET', 'POST'])
def student_login():
    return redirect(url_for('login'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    return redirect(url_for('login'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin/logout')
def admin_logout():
    return redirect(url_for('logout'))

@app.route('/admin')
@admin_required
def admin():
    return render_template('admin.html')

@app.route('/admin/database')
@admin_required
def admin_database():
    def to_value(value):
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    try:
        with database.get_db() as db:
            students = [
                {column.name: to_value(getattr(row, column.name)) for column in database.Student.__table__.columns}
                for row in db.query(database.Student).order_by(database.Student.id).all()
            ]
            admins = [
                {column.name: to_value(getattr(row, column.name)) for column in database.Admin.__table__.columns}
                for row in db.query(database.Admin).order_by(database.Admin.id).all()
            ]
            exams = [
                {column.name: to_value(getattr(row, column.name)) for column in database.Exam.__table__.columns}
                for row in db.query(database.Exam).order_by(database.Exam.id).all()
            ]
            exam_sessions = [
                {column.name: to_value(getattr(row, column.name)) for column in database.ExamSession.__table__.columns}
                for row in db.query(database.ExamSession).order_by(database.ExamSession.start_time.desc()).all()
            ]
            cheating_logs = [
                {column.name: to_value(getattr(row, column.name)) for column in database.CheatingLog.__table__.columns}
                for row in db.query(database.CheatingLog).order_by(database.CheatingLog.timestamp.desc()).all()
            ]
            warnings = [
                {column.name: to_value(getattr(row, column.name)) for column in database.Warning.__table__.columns}
                for row in db.query(database.Warning).order_by(database.Warning.timestamp.desc()).all()
            ]
            admin_activity_logs = [
                {column.name: to_value(getattr(row, column.name)) for column in database.AdminActivityLog.__table__.columns}
                for row in db.query(database.AdminActivityLog).order_by(database.AdminActivityLog.timestamp.desc()).all()
            ]
    except Exception as e:
        print(f"[DB] Database viewer error: {e}")
        students = admins = exams = exam_sessions = cheating_logs = warnings = admin_activity_logs = []

    # ── Summary statistics ────────────────────────────────────────────────────
    try:
        summary = {
            'total_admins':       database.count_total_admins(),
            'total_students':     database.count_total_students(),
            'total_exams':        database.count_total_exams(),
            'total_sessions':     len(exam_sessions),
            'active_sessions':    database.count_active_sessions(),
            'completed_sessions': database.count_completed_sessions(),
            'not_attended':       database.count_not_attended_students(),
            'total_cheating':     database.count_cheating_events(),
            'total_warnings':     database.count_warnings(),
        }
    except Exception as e:
        print(f"[DB] Summary stats error: {e}")
        summary = {
            'total_admins': 0, 'total_students': 0, 'total_exams': 0,
            'total_sessions': 0, 'active_sessions': 0, 'completed_sessions': 0,
            'not_attended': 0, 'total_cheating': 0, 'total_warnings': 0,
        }

    return render_template(
        'admin_database.html',
        students=students,
        admins=admins,
        exams=exams,
        exam_sessions=exam_sessions,
        cheating_logs=cheating_logs,
        warnings=warnings,
        admin_activity_logs=admin_activity_logs,
        summary=summary,
    )

@app.route('/video_feed')
def video_feed():
    # Attempt to stream for current student if logged in
    user = session.get('username')
    return Response(generate_frames(user), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/admin/stream/<username>')
@admin_required
def admin_video_feed(username):
    return Response(generate_frames(username), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stream/upload', methods=['POST'])
@student_required
def upload_frame():
    global PROCESSED_STREAMS
    file = request.files.get('frame')
    if not file:
        return jsonify({"error": "No frame"}), 400
    
    username = session.get('username')
    nparr = np.frombuffer(file.read(), np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if frame is not None:
        # Process frame
        if vision_monitor:
            processed, infractions, evidence = vision_monitor.process_frame(frame)
            for inf in infractions:
                log_infraction(f"Vision: {inf}", evidence)
        else:
            processed, infractions, evidence = frame, [], []
        
        # Store for admin view
        _, buffer = cv2.imencode('.jpg', processed)
        PROCESSED_STREAMS[username] = buffer.tobytes()
        return jsonify({"status": "ok", "infractions": infractions})
    
    return jsonify({"error": "Decode failed"}), 500

@app.route('/api/stats')
def get_stats():
    active_sessions_count = 0
    total_db_warnings = 0
    avg_integrity_score = 0.0
    activity_logs = []
    try:
        active_sessions_count = database.count_active_sessions()
        total_db_warnings = database.count_warnings()
        avg_integrity_score = database.avg_integrity_score()
        activity_logs = database.list_admin_activity_logs(limit=20)
    except Exception as e:
        print(f"[DB] Stats query error: {e}")

    return jsonify({
        "warnings":          cheat_stats["warnings"],
        "total_warnings":    total_db_warnings,
        "active_sessions":   active_sessions_count,
        "avg_integrity_score": round(avg_integrity_score, 1),
        "admin_activity_logs": activity_logs,
        "tab_switches":      cheat_stats.get("tab_switches", 0),
        "tab_switch_limit":  TAB_SWITCH_LIMIT,
        "face_mismatch_count": cheat_stats.get("face_mismatch_count", 0),
        "events":            cheat_stats["events"],
        "evidence":          cheat_stats["evidence"],
        "is_active":         is_exam_active,
        "student":           cheat_stats.get("student", ""),
        "vision_status":     vision_monitor.current_status if vision_monitor else "Offline",
        "audio_status":      "Active" if (audio_monitor and audio_monitor.running) else "Offline",
        "os_status":         "Active" if (os_monitor and os_monitor.running) else "Offline",
        "keystroke_status":  os_monitor.keystroke_status if os_monitor else "Offline",
        "auto_stop_limit":   AUTO_STOP_LIMIT,
    })

@app.route('/api/summary')
def get_summary():
    """Build a rich exam summary report."""
    events = cheat_stats["events"]
    evidence = cheat_stats["evidence"]
    warnings = cheat_stats["warnings"]

    # Severity classification
    HIGH_KEYWORDS = ["phone", "multiple people", "auto-stop"]
    MED_KEYWORDS  = ["looking", "audio", "window"]

    def classify(msg):
        m = msg.lower()
        if any(k in m for k in HIGH_KEYWORDS): return "high"
        if any(k in m for k in MED_KEYWORDS):  return "medium"
        return "low"

    timeline = [
        {**ev, "severity": classify(ev["msg"])}
        for ev in events
    ]

    # Verdict
    high_count = sum(1 for t in timeline if t["severity"] == "high")
    if warnings == 0:
        verdict = "pass"
    elif high_count >= 3 or warnings >= AUTO_STOP_LIMIT:
        verdict = "fail"
    else:
        verdict = "review"

    return jsonify({
        "total_warnings":    warnings,
        "total_evidence":    len(evidence),
        "high_severity":     high_count,
        "medium_severity":   sum(1 for t in timeline if t["severity"] == "medium"),
        "low_severity":      sum(1 for t in timeline if t["severity"] == "low"),
        "verdict":           verdict,
        "timeline":          timeline,
        "evidence":          evidence,
        "submitted":         cheat_stats.get("submitted", False),
    })

@app.route('/api/submit', methods=['POST'])
def submit_report():
    """Mark the exam report as submitted."""
    cheat_stats["submitted"] = True
    print("[Admin] Exam report submitted.")
    return jsonify({"status": "submitted"})


# ── Active Students (live streams) ────────────────────────────────────────────
@app.route('/api/active_students')
@admin_required
def active_students():
    """Return list of students currently streaming (admin only)."""
    result = []
    for username, frame in PROCESSED_STREAMS.items():
        result.append({
            "username": username,
            "warnings": cheat_stats.get("warnings", 0)
               if cheat_stats.get("student") == username else 0,
            "tab_switches": cheat_stats.get("tab_switches", 0)
               if cheat_stats.get("student") == username else 0,
            "is_active": is_exam_active and cheat_stats.get("student") == username,
        })
    return jsonify(result)


@app.route('/api/admin/metrics')
@admin_required
def admin_metrics():
    try:
        completed = database.count_completed_sessions()
        not_attended = database.count_not_attended_students()
        return jsonify({
            'total_students':     database.count_total_students(),
            'total_admins':       database.count_total_admins(),
            'total_exams':        database.count_total_exams(),
            'active_exams':       database.count_active_sessions(),
            'completed_exams':    completed,
            'not_attended':       not_attended,
            'cheating_events':    database.count_cheating_events(),
            'total_warnings':     database.count_warnings(),
        })
    except Exception as e:
        print(f"[DB] Admin metrics error: {e}")
        return jsonify({
            'total_students': 0,
            'total_admins': 0,
            'total_exams': 0,
            'active_exams': 0,
            'completed_exams': 0,
            'not_attended': 0,
            'cheating_events': 0,
            'total_warnings': 0,
        }), 500


@app.route('/api/control', methods=['POST'])
def control_exam():
    global is_exam_active, audio_monitor, os_monitor, cheat_stats

    data = request.json
    if data.get('action') == 'start':
        is_exam_active = True
        session_id = uuid.uuid4().hex
        username = data.get('student', '') or session.get('username', 'Unknown')
        cheat_stats = {"warnings": 0, "face_mismatch_count": 0, "tab_switches": 0, "events": [], "evidence": [],
                       "student": username,
                       "started_at": int(time.time()),
                       "session_id": session_id}
        # Reset per-type cooldown table for the fresh exam session
        _WARN_COOLDOWNS.clear()

        # Tell vision monitor which student is enrolled so it can compare faces
        enrolled_user = username
        if vision_monitor:
            vision_monitor.set_enrolled_username(enrolled_user)

        try:
            student = database.get_student_by_name(username)
            if student is None:
                student = database.create_or_update_student(username, generate_password_hash('default-pass'))
            database.create_exam_session(
                session_id=session_id,
                student_id=student.id if student else None,
                start_time=datetime.utcfromtimestamp(cheat_stats['started_at']),
                status='ongoing'
            )
        except Exception as e:
            print(f"[DB] Create exam session error: {e}")

        if audio_monitor is None or not audio_monitor.running:
            audio_monitor = AudioMonitor(
                callback=lambda msg: log_infraction(f"Audio: {msg}"))
            audio_monitor.start()

        if os_monitor is None or not os_monitor.running:
            os_monitor = OSMonitor(
                callback=lambda msg: log_infraction(f"OS: {msg}"))
            os_monitor.start()

        return jsonify({"status": "Started"})

    elif data.get('action') == 'stop':
        stop_exam()
        return jsonify({"status": "Stopped"})

    return jsonify({"error": "Invalid action"}), 400

@app.route('/static/evidence/<filename>')
def serve_evidence(filename):
    return send_from_directory(EVIDENCE_DIR, filename)

@app.route('/api/evidence')
def list_evidence():
    return jsonify(cheat_stats["evidence"])

@app.route('/api/evidence/<filename>', methods=['DELETE'])
def delete_evidence(filename):
    """Delete an evidence file from disk and the current session list."""
    # 1. Remove from current session list
    cheat_stats["evidence"] = [e for e in cheat_stats["evidence"] if e["file"] != filename]
    
    # 2. Try to delete from disk
    path = os.path.join(EVIDENCE_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
            print(f"[Admin] Evidence deleted: {filename}")
            return jsonify({"status": "deleted"})
        except Exception as e:
            print(f"[Admin] Error deleting file {filename}: {e}")
            return jsonify({"error": str(e)}), 500
    
    return jsonify({"status": "removed from list, file not found on disk"})

@app.route('/api/set_baseline', methods=['POST'])
def set_baseline():
    """Store browser baseline and apply it immediately to os_monitor."""
    data = request.json or {}
    key_count  = int(data.get('keyCount', 0))
    duration   = int(data.get('duration', 30))
    student    = data.get('student', session.get('username', 'Unknown'))
    cheat_stats["browser_baseline"] = {
        "student":      student,
        "keyCount":     key_count,
        "mouseMoves":   data.get('mouseMoves', 0),
        "duration":     duration,
        "avgDwell":     data.get('avgDwell', 0),
        "stdDwell":     data.get('stdDwell', 0),
        "avgFlight":    data.get('avgFlight', 0),
        "stdFlight":    data.get('stdFlight', 0),
        "avgRate":      data.get('avgRate', 0),
    }
    # Wire baseline into running os_monitor immediately
    if os_monitor and os_monitor.running:
        os_monitor.set_browser_baseline(key_count, duration)

    print(f"[Baseline] Set for {student}: {key_count} keys/{duration}s "
          f"dwell={data.get('avgDwell',0):.0f}ms flight={data.get('avgFlight',0):.0f}ms")
    return jsonify({"status": "ok"})

@app.route('/api/report_keystroke', methods=['POST'])
def report_keystroke():
    """Receive per-keystroke dwell/flight timings from the exam page."""
    if not is_exam_active:
        return jsonify({"status": "ignored"})
    data       = request.json or {}
    dwell_ms   = float(data.get('dwell_ms', 0))
    flight_ms  = float(data.get('flight_ms', 0))
    if os_monitor and os_monitor.running:
        os_monitor.receive_keystroke_event(dwell_ms, flight_ms)
    return jsonify({"status": "ok"})

@app.route('/api/report_event', methods=['POST'])
def report_event():
    """Receive browser-side events (tab switch) from the student page."""
    global cheat_stats
    data = request.json or {}
    msg  = data.get('msg', 'Unknown browser event')
    if not is_exam_active:
        return jsonify({"status": "ignored"})

    is_tab_event = 'tab' in msg.lower() or 'focus' in msg.lower() or 'visibility' in msg.lower()
    is_shortcut  = 'shortcut' in msg.lower()

    if is_tab_event or is_shortcut:
        cheat_stats["tab_switches"] = cheat_stats.get("tab_switches", 0) + 1
        switch_count = cheat_stats["tab_switches"]
        username = session.get('username')
        
        # Capture evidence from the latest uploaded frame if possible
        evidence_file = None
        if username in PROCESSED_STREAMS:
            # We already have a processed frame in buffer, use it as evidence
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            evidence_file = f"evidence_{ts}_tab_switch.jpg"
            path = os.path.join(EVIDENCE_DIR, evidence_file)
            with open(path, 'wb') as f:
                f.write(PROCESSED_STREAMS[username])
        elif vision_monitor:
            # Fallback (though we want to avoid server-side camera)
            evidence_file = vision_monitor.capture_event_frame(msg)
            
        log_infraction(f"Browser: {msg}", evidence_file)

        if switch_count >= TAB_SWITCH_LIMIT:
            # Enough tab switches — terminate exam
            stop_exam()
            return jsonify({
                "status":       "stopped",
                "reason":       msg,
                "switch_count": switch_count,
                "limit":        TAB_SWITCH_LIMIT,
            })
        else:
            # Warn but keep exam going
            return jsonify({
                "status":       "warned",
                "switch_count": switch_count,
                "limit":        TAB_SWITCH_LIMIT,
                "remaining":    TAB_SWITCH_LIMIT - switch_count,
            })
    else:
        log_infraction(f"Browser: {msg}")

    return jsonify({"status": "logged"})

@app.route('/api/sessions')
@admin_required
def list_sessions():
    """List all saved past exam sessions, newest first."""
    try:
        sessions = database.list_exam_sessions()
        return jsonify(sessions)
    except Exception as e:
        print(f"[DB] List sessions error: {e}")

    sessions = []
    for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if fname.endswith('.json'):
            path = os.path.join(SESSIONS_DIR, fname)
            try:
                with open(path) as f:
                    s = json.load(f)
                sessions.append({
                    "id":             s.get("id", fname),
                    "filename":       fname,
                    "ended_at":       s.get("ended_at", 0),
                    "student":        s.get("student", "Unknown"),
                    "warnings":       s.get("warnings", 0),
                    "evidence_count": len(s.get("evidence", [])),
                })
            except Exception:
                pass
    return jsonify(sessions)

@app.route('/api/sessions', methods=['DELETE'])
@admin_required
def delete_all_sessions():
    """Delete ALL saved session records."""
    deleted = 0
    errors  = []
    try:
        deleted = database.delete_all_exam_sessions()
    except Exception as e:
        errors.append(str(e))

    for fname in os.listdir(SESSIONS_DIR):
        if fname.endswith('.json'):
            try:
                os.remove(os.path.join(SESSIONS_DIR, fname))
                deleted += 1
            except Exception as e:
                errors.append(str(e))
    print(f"[Admin] Deleted {deleted} session record(s).")
    return jsonify({"status": "deleted", "count": deleted, "errors": errors})

@app.route('/api/sessions/<session_id>', methods=['DELETE'])
@admin_required
def delete_session(session_id):
    """Delete a single session record by ID."""
    try:
        deleted = database.delete_exam_session(session_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if deleted:
        fname = f"session_{session_id}.json"
        path  = os.path.join(SESSIONS_DIR, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        print(f"[Admin] Session deleted: {session_id}")
        return jsonify({"status": "deleted"})

    return jsonify({"error": "Not found"}), 404

@app.route('/api/status')
def get_status():
    # We need a fallback if session is dead, but let's assume it's ScoreHunt
    return jsonify({"server": "ScoreHunt AI Proctorer", "status": "online"})


@app.route('/api/db/test')
def db_test():
    """Diagnostic endpoint — verifies DB reads and writes work correctly.
    Call this URL in browser: http://127.0.0.1:5000/api/db/test
    """
    results = {}
    # 1. Count existing records
    try:
        results['total_students'] = database.count_total_students()
        results['total_admins']   = database.count_total_admins()
        results['total_sessions'] = database.count_active_sessions()
        results['total_cheating'] = database.count_cheating_events()
        results['count_ok'] = True
    except Exception as e:
        results['count_ok'] = False
        results['count_error'] = str(e)

    # 2. Write a test student (will update if exists)
    try:
        from werkzeug.security import generate_password_hash
        test_student = database.create_or_update_student(
            '__db_test_user__',
            generate_password_hash('test123'),
            email='test@test.com',
            year='Test',
            branch='TestBranch',
        )
        results['write_ok']       = True
        results['test_student_id'] = test_student.id if test_student else None
    except Exception as e:
        results['write_ok']    = False
        results['write_error'] = str(e)

    # 3. Re-count after write
    try:
        results['total_students_after'] = database.count_total_students()
    except Exception as e:
        results['total_students_after'] = f'error: {e}'

    # 4. Also re-sync users.json → DB
    try:
        _sync_users_to_db()
        results['sync_ok'] = True
    except Exception as e:
        results['sync_ok']    = False
        results['sync_error'] = str(e)

    # 5. Final counts
    try:
        results['final_students'] = database.count_total_students()
        results['final_admins']   = database.count_total_admins()
    except Exception as e:
        results['final_error'] = str(e)

    print(f"[DB Test] Results: {results}")
    return jsonify(results)

@app.route('/api/sessions/<session_id>')
@admin_required
def get_session(session_id):
    """Return full data for a specific past session."""
    try:
        data = database.get_exam_session_details(session_id)
        if data:
            return jsonify(data)
    except Exception as e:
        print(f"[DB] Get session error: {e}")

    fname = f"session_{session_id}.json"
    path  = os.path.join(SESSIONS_DIR, fname)
    if not os.path.exists(path):
        return jsonify({"error": "Not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))

# ── User Management ───────────────────────────────────────────────────────────
@app.route('/api/users/students', methods=['GET'])
@admin_required
def get_students():
    """List all registered students."""
    students = []
    try:
        with database.get_db() as db:
            rows = db.query(database.Student).order_by(database.Student.created_at.desc()).all()
            students = [
                {
                    "username": st.name,
                    "email": st.email,
                    "year": st.year,
                    "branch": st.branch,
                    "created_at": int(st.created_at.timestamp()) if st.created_at else 0,
                }
                for st in rows
            ]
        return jsonify(students)
    except Exception as e:
        print(f"[DB] Get students error: {e}")

    users = load_users()
    students = [
        {"username": uname, "role": data.get("role")}
        for uname, data in users.items() if data.get("role") == "student"
    ]
    return jsonify(students)

@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def delete_user(username):
    """Delete a user account."""
    if username == session.get('username'):
        return jsonify({"error": "Cannot delete yourself"}), 400
        
    users = load_users()
    if username not in users:
        return jsonify({"error": "User not found"}), 404
        
    del users[username]
    save_users(users)
    try:
        database.delete_student_by_name(username)
    except Exception as e:
        print(f"[DB] Delete student error: {e}")
    print(f"[Admin] User deleted: {username}")
    log_admin_action('deleted_student', f'Deleted student account: {username}')
    return jsonify({"status": "deleted"})

# ── Question Bank API ─────────────────────────────────────────────────────────
@app.route('/api/questions')
def get_questions():
    """Return all questions for the exam (students use this)."""
    questions = legacy_db.list_questions()
    if not questions:
        # Fallback: return built-in sample questions so exam stays functional
        return jsonify([{
            "id": 1, "type": "mcq",
            "question": "What does AI stand for?",
            "options": ["Artificial Intelligence","Automated Interface",
                        "Analytical Insight","Applied Integration"],
            "correct_answer": 0,
            "placeholder": None, "code_prompt": None
        }])
    return jsonify(questions)


@app.route('/api/admin/questions', methods=['GET'])
@admin_required
def admin_list_questions():
    """Return all questions (admin view, includes correct answers)."""
    return jsonify(legacy_db.list_questions())


@app.route('/api/admin/questions', methods=['POST'])
@admin_required
def admin_add_question():
    """Add a new question to the bank."""
    data = request.json or {}
    q_type = data.get('type', 'mcq')
    question = data.get('question', '').strip()
    if not question:
        return jsonify({"error": "Question text required"}), 400

    options        = data.get('options')         # list of strings for MCQ
    correct_answer = data.get('correct_answer')  # int index for MCQ
    code_prompt    = data.get('code_prompt')     # for code type
    placeholder    = data.get('placeholder')

    new_id = legacy_db.add_question(
        q_type=q_type,
        question=question,
        options=options,
        correct_answer=correct_answer,
        code_prompt=code_prompt,
        placeholder=placeholder,
    )
    print(f"[Admin] Question added (id={new_id}): {q_type} — {question[:40]}")
    return jsonify({"status": "created", "id": new_id}), 201


@app.route('/api/admin/questions/<int:q_id>', methods=['DELETE'])
@admin_required
def admin_delete_question(q_id):
    """Delete a question by id."""
    deleted = legacy_db.delete_question(q_id)
    if deleted:
        return jsonify({"status": "deleted"})
    return jsonify({"error": "Not found"}), 404


# ── Face Recognition API ──────────────────────────────────────────────────────
@app.route('/api/face/enroll', methods=['POST'])
@student_required
def face_enroll():
    """Receive one or more JPEG frames from the verify_face page and enroll the student."""
    if face_recognizer is None:
        return jsonify({"error": "Face recognition not available"}), 503

    username = session.get('username')
    files = request.files.getlist('frame')  # supports multi-frame upload
    if not files:
        return jsonify({"error": "No frames provided"}), 400

    frames = []
    for f in files:
        nparr = np.frombuffer(f.read(), np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is not None:
            frames.append(frame)

    if not frames:
        return jsonify({"error": "Could not decode frames"}), 400

    success = face_recognizer.enroll(username, *frames)
    if success:
        print(f"[Face] Enrolled {username} with {len(frames)} frame(s).")
        return jsonify({"status": "enrolled", "username": username})
    else:
        return jsonify({"error": "No face detected in frames — please ensure your face is visible and well-lit"}), 422


@app.route('/api/face/status')
@student_required
def face_status():
    """Check whether the current student has an enrolled face model."""
    if face_recognizer is None:
        return jsonify({"enrolled": False, "reason": "unavailable"})
    username = session.get('username')
    enrolled = face_recognizer.is_enrolled(username)
    return jsonify({"enrolled": enrolled, "username": username})


@app.route('/api/face/verify', methods=['POST'])
@student_required
def face_verify_api():
    """Quick one-shot verification used on the verify_id page."""
    if face_recognizer is None:
        return jsonify({"match": True, "reason": "unavailable"})  # passthrough
    username = session.get('username')
    if not face_recognizer.is_enrolled(username):
        return jsonify({"match": False, "reason": "not_enrolled"})

    f = request.files.get('frame')
    if not f:
        return jsonify({"error": "No frame"}), 400
    nparr = np.frombuffer(f.read(), np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "Decode failed"}), 400

    match, conf = face_recognizer.verify(username, frame)
    return jsonify({"match": match, "confidence": round(conf, 1)})


if __name__ == "__main__":
    print("Starting ScoreHunt AI Proctor Server...")
    app.run(debug=True, host='0.0.0.0', threaded=True, port=5000, use_reloader=False)
