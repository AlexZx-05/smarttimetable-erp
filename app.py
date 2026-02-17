from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import timetable
import os
from datetime import datetime
import json
import uuid

app = Flask(__name__)
app.secret_key = "very-secret-key"
print("SMART TIMETABLE SERVER STARTED")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.txt")
PENDING_FILE = os.path.join(BASE_DIR, "users_pending.txt")
DATA_FILE = os.path.join(BASE_DIR, "data.txt")
TIMETABLE_FILE = os.path.join(BASE_DIR, "timetable_output.txt")
HISTORY_FILE = os.path.join(BASE_DIR, "approval_history.txt")
PREFERENCE_REQUESTS_FILE = os.path.join(BASE_DIR, "preference_requests.txt")
PREFERENCE_HISTORY_FILE = os.path.join(BASE_DIR, "preference_history.txt")
MAX_ROOM_CAPACITY = 50
PROFILE_UPLOAD_DIR = os.path.join(BASE_DIR, "static", "profile_pics")
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
EVENTS_FILE = os.path.join(BASE_DIR, "events.txt")
TIMETABLE_HISTORY_FILE = os.path.join(BASE_DIR, "timetable_history.txt")
SEMESTER_OPTIONS = [
    ("jan_apr", "Jan-Apr Semester"),
    ("aug_nov", "Aug-Nov Semester"),
    ("dec_vacation", "December Vacation"),
    ("jan_may", "Jan-May Semester")
]


def infer_default_semester_key(month):
    if month in (1, 2, 3, 4):
        return "jan_apr"
    if month in (8, 9, 10, 11):
        return "aug_nov"
    if month == 12:
        return "dec_vacation"
    return "jan_may"


def build_semester_label(key, year):
    year_str = str(year).strip()
    labels = {
        "jan_apr": f"{year_str} Jan-Apr Semester",
        "aug_nov": f"{year_str} Aug-Nov Semester",
        "dec_vacation": f"{year_str} December Vacation",
        "jan_may": f"{year_str} Jan-May Semester"
    }
    return labels.get(key, f"{year_str} Jan-Apr Semester")


def append_line_safe(file_path, line):
    # Ensure appended records always start on a new line.
    needs_newline = False
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")

    with open(file_path, "a") as f:
        if needs_newline:
            f.write("\n")
        f.write(line.rstrip("\n") + "\n")


def parse_user_line(line):
    parts = line.strip().split(",")
    if len(parts) < 4:
        return None
    return {
        "email": parts[0],
        "hash": parts[1],
        "role": parts[2],
        "name": parts[3],
        "department": parts[4] if len(parts) >= 5 else "ALL",
        "profile_pic": parts[5] if len(parts) >= 6 else ""
    }


def parse_pending_line(line):
    parts = line.strip().split(",")
    if len(parts) < 5:
        return None
    return {
        "email": parts[0],
        "name": parts[1],
        "department": parts[2],
        "role": parts[3],
        "hash": parts[4]
    }


def parse_course_line(line):
    parts = line.strip().split(",")
    if len(parts) < 6:
        return None

    subject = parts[0]
    teacher = parts[1]
    students = parts[2]

    # Backward-compatible: old file has no target department.
    if ":" in parts[3]:
        target = "ALL"
        prefs = parts[3:]
    else:
        target = parts[3] if parts[3] else "ALL"
        prefs = parts[4:]

    prefs = (prefs + ["-:-", "-:-", "-:-"])[:3]

    return {
        "subject": subject,
        "teacher": teacher,
        "students": students,
        "target": target,
        "prefs": prefs
    }


def serialize_course(course):
    return ",".join([
        course["subject"],
        course["teacher"],
        str(course["students"]),
        course.get("target", "ALL"),
        course["prefs"][0],
        course["prefs"][1],
        course["prefs"][2]
    ])


def parse_preference_request_line(line):
    parts = line.strip().split(",")
    if len(parts) < 8:
        return None
    return {
        "id": parts[0],
        "subject": parts[1],
        "teacher": parts[2],
        "students": parts[3],
        "target": parts[4],
        "prefs": [parts[5], parts[6], parts[7]]
    }


def serialize_preference_request(req):
    return ",".join([
        req["id"],
        req["subject"],
        req["teacher"],
        str(req["students"]),
        req.get("target", "ALL"),
        req["prefs"][0],
        req["prefs"][1],
        req["prefs"][2]
    ])


def load_preference_requests():
    requests = []
    if os.path.exists(PREFERENCE_REQUESTS_FILE):
        with open(PREFERENCE_REQUESTS_FILE) as f:
            for line in f:
                parsed = parse_preference_request_line(line)
                if parsed:
                    requests.append(parsed)
    return requests


def save_preference_requests(requests):
    with open(PREFERENCE_REQUESTS_FILE, "w") as f:
        for req in requests:
            f.write(serialize_preference_request(req) + "\n")


def load_courses():
    courses = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            for line in f:
                parsed = parse_course_line(line)
                if parsed:
                    courses.append(parsed)
    return courses


def save_courses(courses):
    with open(DATA_FILE, "w") as f:
        for course in courses:
            f.write(serialize_course(course) + "\n")


def normalize_prefs(pref_list):
    cleaned = []
    for p in pref_list:
        p = (p or "").strip()
        if not p or p == "-:-":
            continue
        if p not in cleaned:
            cleaned.append(p)
    return (cleaned + ["-:-", "-:-", "-:-"])[:3]


def find_course(courses, subject, teacher, target):
    for c in courses:
        if (
            c.get("subject", "") == subject
            and c.get("teacher", "") == teacher
            and c.get("target", "ALL") == target
        ):
            return c
    return None


def apply_timetable_delete(day, slot, subject, room, teacher, target):
    rows = load_timetable_rows()
    filtered = []
    deleted = False

    for row in rows:
        match = (
            row["day"] == day
            and row["slot"] == slot
            and row["subject"] == subject
            and row["room"] == room
            and row.get("teacher", "") == teacher
            and row.get("target", "ALL") == target
        )
        if match and not deleted:
            deleted = True
            continue
        filtered.append(row)

    if deleted:
        save_timetable_rows(filtered)

        # Keep generation source in sync.
        courses = load_courses()
        course = find_course(courses, subject, teacher, target or "ALL")
        if course:
            old_pref = f"{day}:{slot}"
            prefs = [p for p in course.get("prefs", []) if p != "-:-"]
            if old_pref in prefs:
                prefs.remove(old_pref)
                course["prefs"] = normalize_prefs(prefs)
                save_courses(courses)

    return deleted


def apply_timetable_update(old_row, new_row):
    rows = load_timetable_rows()
    target_index = -1
    for i, row in enumerate(rows):
        if (
            row.get("day", "") == old_row["day"]
            and row.get("slot", "") == old_row["slot"]
            and row.get("subject", "") == old_row["subject"]
            and row.get("room", "") == old_row["room"]
            and row.get("teacher", "") == old_row["teacher"]
            and row.get("target", "ALL") == old_row["target"]
        ):
            target_index = i
            break

    if target_index == -1:
        return False

    rows[target_index] = new_row
    save_timetable_rows(rows)

    # Keep generation source in sync for next "Generate".
    courses = load_courses()
    old_course = find_course(courses, old_row["subject"], old_row["teacher"], old_row["target"] or "ALL")
    new_course = find_course(courses, new_row["subject"], new_row["teacher"], new_row["target"])
    old_pref = f"{old_row['day']}:{old_row['slot']}"
    new_pref = f"{new_row['day']}:{new_row['slot']}"
    changed_courses = False

    if old_course:
        prefs = [p for p in old_course.get("prefs", []) if p != "-:-"]
        if old_pref in prefs:
            prefs.remove(old_pref)
            old_course["prefs"] = normalize_prefs(prefs)
            changed_courses = True

    if new_course:
        prefs = [p for p in new_course.get("prefs", []) if p != "-:-"]
        if new_pref not in prefs:
            if len(prefs) < 3:
                prefs.append(new_pref)
            else:
                prefs[-1] = new_pref
            new_course["prefs"] = normalize_prefs(prefs)
            changed_courses = True

    if changed_courses:
        save_courses(courses)

    return True


def parse_history_line(line):
    parts = line.strip().split(",")
    if len(parts) < 7:
        return None
    return {
        "timestamp": parts[0],
        "action": parts[1],
        "email": parts[2],
        "name": parts[3],
        "department": parts[4],
        "role": parts[5],
        "admin": parts[6]
    }


def log_admin_action(action, pending_user):
    admin_email = session.get("email", "admin")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_line_safe(
        HISTORY_FILE,
        (
            f"{timestamp},{action},{pending_user['email']},"
            f"{pending_user['name']},{pending_user['department']},"
            f"{pending_user['role']},{admin_email}"
        )
    )


def parse_preference_history_line(line):
    parts = line.strip().split(",")
    if len(parts) < 6:
        return None
    return {
        "timestamp": parts[0],
        "action": parts[1],
        "subject": parts[2],
        "teacher": parts[3],
        "target": parts[4],
        "admin": parts[5]
    }


def log_preference_action(action, request_data):
    admin_email = session.get("email", "admin")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_line_safe(
        PREFERENCE_HISTORY_FILE,
        (
            f"{timestamp},{action},{request_data['subject']},"
            f"{request_data['teacher']},{request_data['target']},{admin_email}"
        )
    )


def load_timetable_rows():
    rows = []
    if os.path.exists(TIMETABLE_FILE):
        with open(TIMETABLE_FILE) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 4:
                    continue
                rows.append({
                    "day": parts[0],
                    "slot": parts[1],
                    "subject": parts[2],
                    "room": parts[3],
                    "teacher": parts[4] if len(parts) >= 5 else "",
                    "target": parts[5] if len(parts) >= 6 else "ALL",
                    "label": parts[6] if len(parts) >= 7 else ""
                })
    return rows


def save_timetable_rows(rows):
    with open(TIMETABLE_FILE, "w") as f:
        for row in rows:
            base = [
                row.get("day", ""),
                row.get("slot", ""),
                row.get("subject", ""),
                row.get("room", ""),
                row.get("teacher", ""),
                row.get("target", "ALL")
            ]
            label = row.get("label", "").strip()
            if label:
                base.append(label)
            f.write(",".join(base) + "\n")


def load_users():
    users = []
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                parsed = parse_user_line(line)
                if parsed:
                    users.append(parsed)
    return users


def save_users(users):
    with open(USERS_FILE, "w") as f:
        for u in users:
            department = u.get("department", "ALL") or "ALL"
            profile_pic = u.get("profile_pic", "")
            f.write(
                f"{u['email']},{u['hash']},{u['role']},"
                f"{u['name']},{department},{profile_pic}\n"
            )


def load_events():
    events = []
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if "id" in e and "title" in e and "date" in e:
                        events.append(e)
                except json.JSONDecodeError:
                    continue
    return events


def save_events(events):
    with open(EVENTS_FILE, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def load_timetable_history():
    history = []
    if os.path.exists(TIMETABLE_HISTORY_FILE):
        with open(TIMETABLE_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if "semester" in row and "generated_at" in row:
                        history.append(row)
                except json.JSONDecodeError:
                    continue
    history.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return history


def group_timetable_history_by_semester(history_rows):
    grouped_map = {}
    order = []
    for h in history_rows:
        sem = h.get("semester", "Unknown Semester")
        if sem not in grouped_map:
            grouped_map[sem] = []
            order.append(sem)
        grouped_map[sem].append(h)

    grouped = []
    for sem in order:
        runs = grouped_map[sem]
        latest = runs[0] if runs else {}
        grouped.append({
            "semester": sem,
            "latest_generated_at": latest.get("generated_at", ""),
            "latest_rows": latest.get("total_rows", 0),
            "latest_subjects": latest.get("subjects", []),
            "generated_by": latest.get("generated_by", ""),
            "total_runs": len(runs),
            "runs": runs
        })
    return grouped


def log_timetable_history(semester, generated_by, rows):
    record = {
        "id": str(uuid.uuid4()),
        "semester": semester,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": generated_by,
        "total_rows": len(rows),
        "subjects": sorted(list({r.get("subject", "") for r in rows if r.get("subject", "")})),
        "rows": rows
    }
    append_line_safe(TIMETABLE_HISTORY_FILE, json.dumps(record))


def event_color(event_type):
    palette = {
        "exam": "#dc2626",
        "test": "#f59e0b",
        "vacation": "#16a34a",
        "general": "#2563eb"
    }
    return palette.get(event_type, "#2563eb")


def to_calendar_event(event, viewer_email):
    return {
        "id": event["id"],
        "title": event["title"],
        "start": event["date"],
        "allDay": True,
        "color": event_color(event.get("type", "general")),
        "extendedProps": {
            "type": event.get("type", "general"),
            "important": event.get("important", False),
            "subject": event.get("subject", event.get("title", "")),
            "creator_name": event.get("creator_name", ""),
            "creator_email": event.get("creator_email", ""),
            "is_owner": event.get("creator_email", "") == viewer_email
        }
    }


def get_upcoming_vacations(limit=10):
    today = datetime.now().strftime("%Y-%m-%d")
    vacations = []
    for e in load_events():
        if e.get("type", "") == "vacation" and e.get("date", "") >= today:
            vacations.append(e)
    vacations.sort(key=lambda x: x.get("date", ""))
    return vacations[:limit]


def infer_department_from_email(email):
    prefix = email.split("@")[0].lower()
    if prefix.startswith("cs"):
        return "CSE"
    if prefix.startswith("ec") or prefix.startswith("ece"):
        return "ECE"
    if prefix.startswith("it"):
        return "IT"
    if prefix.startswith("me"):
        return "ME"
    return "ALL"


# =====================================================
# HOME → REDIRECT TO LOGIN
# =====================================================
@app.route("/")
def home():
    return redirect("/login")


# =====================================================
# LOGIN (ADMIN / TEACHER / STUDENT)
# =====================================================
@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":
        return render_template(
            "login.html",
            message=request.args.get("message", ""),
            error=request.args.get("error", "")
        )

    # ---------------- ADMIN / TEACHER ----------------
    role = request.form.get("role", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if not os.path.exists(USERS_FILE):
        return redirect("/login?error=No+users+found.+Admin+must+create+accounts.")

    with open(USERS_FILE) as f:
        for line in f:
            user = parse_user_line(line)
            if not user:
                continue

            if (
                user["email"] == email
                and user["role"] == role
                and check_password_hash(user["hash"], password)
            ):
                session["email"] = user["email"]
                session["role"] = user["role"]
                session["name"] = user["name"]
                dept = user.get("department", "ALL")
                if role == "student" and (not dept or dept.upper() == "ALL"):
                    dept = infer_department_from_email(user["email"])
                session["department"] = dept
                session["profile_pic"] = user.get("profile_pic", "")

                if role == "admin":
                    return redirect("/admin/dashboard")
                if role == "teacher":
                    return redirect("/teacher/dashboard")
                if role == "student":
                    return redirect("/student/dashboard")

    return redirect("/login?error=Invalid+credentials+or+not+approved+yet.")


# =====================================================
# SIGNUP REQUEST (TEACHER / STUDENT)
# =====================================================
@app.route("/signup", methods=["POST"])
def signup():

    role = request.form.get("role", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    department = request.form.get("department", "").strip()
    password = request.form.get("password", "").strip()

    if role not in ("teacher", "student"):
        return redirect("/login?error=Please+select+Teacher+or+Student+for+signup.")

    if not name or not email or not department or not password:
        return redirect("/login?error=All+signup+fields+are+required.")

    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                user = parse_user_line(line)
                if user and user["email"] == email and user["role"] == role:
                    return redirect("/login?error=Account+already+exists.+Please+login.")

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if pending and pending["email"] == email and pending["role"] == role:
                    return redirect("/login?error=Signup+request+already+pending+admin+approval.")

    hashed = generate_password_hash(password)
    append_line_safe(PENDING_FILE, f"{email},{name},{department},{role},{hashed}")

    return redirect("/login?message=Signup+request+submitted.+Wait+for+admin+approval.")


# =====================================================
# LOGOUT
# =====================================================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# =====================================================
# ADMIN DASHBOARD
# =====================================================
@app.route("/admin/dashboard")
def admin_dashboard():

    if session.get("role") != "admin":
        return redirect("/login")

    pending = []
    history = []
    preference_requests = load_preference_requests()
    preference_history = []
    timetable_rows = load_timetable_rows()
    timetable_history = load_timetable_history()
    timetable_history_grouped = group_timetable_history_by_semester(timetable_history)
    now = datetime.now()
    default_semester_key = infer_default_semester_key(now.month)
    default_semester_year = now.year
    admin_events = load_events()
    admin_events.sort(key=lambda x: x.get("date", ""))
    vacations = [e for e in admin_events if e.get("type", "") == "vacation"]
    users = load_users()
    courses = load_courses()
    teacher_cards = []

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                parsed = parse_pending_line(line)
                if parsed:
                    pending.append(parsed)

    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            for line in f:
                parsed = parse_history_line(line)
                if parsed:
                    history.append(parsed)

    if os.path.exists(PREFERENCE_HISTORY_FILE):
        with open(PREFERENCE_HISTORY_FILE) as f:
            for line in f:
                parsed = parse_preference_history_line(line)
                if parsed:
                    preference_history.append(parsed)

    history.reverse()
    preference_history.reverse()

    for user in users:
        if user["role"] != "teacher":
            continue

        teacher_name = user["name"]
        teacher_courses = [c for c in courses if c["teacher"] == teacher_name]
        teacher_rows = [r for r in timetable_rows if r.get("teacher", "") == teacher_name]
        absent_count = len([r for r in teacher_rows if r.get("label", "") == "Teacher Absent"])

        teacher_cards.append({
            "name": teacher_name,
            "email": user["email"],
            "department": user.get("department", "ALL"),
            "profile_pic": user.get("profile_pic", ""),
            "courses": teacher_courses,
            "timetable_count": len(teacher_rows),
            "absent_count": absent_count,
            "is_all_absent": len(teacher_rows) > 0 and absent_count == len(teacher_rows)
        })

    return render_template(
        "admin.html",
        pending=pending,
        history=history,
        preference_requests=preference_requests,
        preference_history=preference_history,
        timetable_rows=timetable_rows,
        timetable_history=timetable_history,
        timetable_history_grouped=timetable_history_grouped,
        approved_courses_count=len(courses),
        approved_course_stack=sorted(courses, key=lambda c: (c.get("teacher", ""), c.get("subject", ""))),
        semester_options=SEMESTER_OPTIONS,
        default_semester_key=default_semester_key,
        default_semester_year=default_semester_year,
        teacher_cards=teacher_cards,
        admin_events=admin_events,
        vacations=vacations,
        admin_name=session.get("name", "Admin"),
        admin_email=session.get("email", ""),
        admin_profile_pic=session.get("profile_pic", ""),
        message=request.args.get("message", ""),
        error=request.args.get("error", "")
    )


# =====================================================
# ADMIN APPROVE TEACHER
# =====================================================
@app.route("/admin/approve")
def approve_teacher():

    if session.get("role") != "admin":
        return "Unauthorized"

    email = request.args.get("email")
    if not email:
        return redirect("/admin/dashboard")

    approved = None
    remaining = []

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if not pending:
                    continue
                if approved is None and pending["email"] == email:
                    approved = pending
                else:
                    remaining.append(line)

        with open(PENDING_FILE, "w") as f:
            f.writelines(remaining)

    if not approved:
        return redirect("/admin/dashboard")

    append_line_safe(
        USERS_FILE,
        (
            f"{approved['email']},{approved['hash']},{approved['role']},"
            f"{approved['name']},{approved['department']}"
        )
    )
    log_admin_action("approved", approved)
    return redirect("/admin/dashboard")


# =====================================================
# ADMIN REJECT
# =====================================================
@app.route("/admin/reject")
def reject_teacher():

    if session.get("role") != "admin":
        return "Unauthorized"

    email = request.args.get("email")
    role = request.args.get("role")
    rejected = None

    if os.path.exists(PENDING_FILE):
        remaining = []
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if not pending:
                    continue
                if (
                    pending["email"] == email
                    and (role is None or pending["role"] == role)
                ):
                    if rejected is None:
                        rejected = pending
                    continue
                else:
                    remaining.append(line)

        with open(PENDING_FILE, "w") as f:
            f.writelines(remaining)

    if rejected:
        log_admin_action("rejected", rejected)

    return redirect("/admin/dashboard")


# =====================================================
# ADMIN PREFERENCE APPROVAL
# =====================================================
@app.route("/admin/preferences/approve")
def approve_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "")
    requests = load_preference_requests()
    approved = None
    remaining = []

    for req in requests:
        if approved is None and req["id"] == request_id:
            approved = req
        else:
            remaining.append(req)

    if not approved:
        return redirect("/admin/dashboard?section=preferences-section")

    try:
        if int(approved["students"]) > MAX_ROOM_CAPACITY:
            return redirect(
                "/admin/dashboard?error=Cannot+approve:+students+exceed+max+room+capacity+(50).+Please+edit+request.&section=preferences-section"
            )
    except ValueError:
        return redirect("/admin/dashboard?error=Invalid+students+count+in+request.&section=preferences-section")

    courses = load_courses()
    upserted = False
    for i, course in enumerate(courses):
        if (
            course["subject"] == approved["subject"]
            and course["teacher"] == approved["teacher"]
        ):
            courses[i] = {
                "subject": approved["subject"],
                "teacher": approved["teacher"],
                "students": approved["students"],
                "target": approved["target"],
                "prefs": approved["prefs"]
            }
            upserted = True
            break

    if not upserted:
        courses.append({
            "subject": approved["subject"],
            "teacher": approved["teacher"],
            "students": approved["students"],
            "target": approved["target"],
            "prefs": approved["prefs"]
        })

    save_courses(courses)
    save_preference_requests(remaining)
    log_preference_action("approved", approved)
    return redirect("/admin/dashboard?message=Preference+approved.+Review+in+Generate+Timetable.&section=generate-section")


@app.route("/admin/preferences/reject")
def reject_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "")
    requests = load_preference_requests()
    rejected = None
    remaining = []

    for req in requests:
        if rejected is None and req["id"] == request_id:
            rejected = req
        else:
            remaining.append(req)

    save_preference_requests(remaining)
    if rejected:
        log_preference_action("rejected", rejected)
    return redirect("/admin/dashboard")


@app.route("/admin/preferences/edit", methods=["GET", "POST"])
def edit_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "").strip()
    requests = load_preference_requests()
    target_req = None
    idx = -1

    for i, req in enumerate(requests):
        if req["id"] == request_id:
            target_req = req
            idx = i
            break

    if target_req is None:
        return redirect("/admin/dashboard")

    if request.method == "POST":
        target_req["subject"] = request.form.get("subject", "").strip()
        target_req["students"] = request.form.get("students", "").strip()
        target_req["target"] = request.form.get("target", "ALL").strip() or "ALL"
        try:
            if int(target_req["students"]) > MAX_ROOM_CAPACITY:
                return redirect(
                    f"/admin/preferences/edit?id={request_id}&error=Students+exceed+max+room+capacity+(50)."
                )
        except ValueError:
            return redirect(f"/admin/preferences/edit?id={request_id}&error=Invalid+students+count.")

        target_req["prefs"] = [
            f"{request.form.get('day1', '-').strip()}:{request.form.get('slot1', '-').strip()}",
            f"{request.form.get('day2', '-').strip()}:{request.form.get('slot2', '-').strip()}",
            f"{request.form.get('day3', '-').strip()}:{request.form.get('slot3', '-').strip()}"
        ]

        # Keep id consistent with teacher+subject.
        target_req["id"] = f"{target_req['teacher']}|{target_req['subject']}".lower()
        requests[idx] = target_req
        save_preference_requests(requests)
        log_preference_action("edited", target_req)
        return redirect("/admin/dashboard")

    day_slot = []
    for pref in target_req["prefs"]:
        if ":" in pref:
            day_slot.append(pref.split(":", 1))
        else:
            day_slot.append(["-", "-"])
    while len(day_slot) < 3:
        day_slot.append(["-", "-"])

    return render_template(
        "admin_edit_preference.html",
        req=target_req,
        day_slot=day_slot,
        error=request.args.get("error", "")
    )


# =====================================================
# GENERATE TIMETABLE
# =====================================================
@app.route("/generate", methods=["POST"])
def generate():

    if session.get("role") != "admin":
        return "Unauthorized"

    semester = request.form.get("semester", "").strip()
    semester_key = request.form.get("semester_key", "").strip()
    semester_year = request.form.get("semester_year", "").strip() or str(datetime.now().year)
    if not semester:
        semester = build_semester_label(semester_key, semester_year)
    ok, msg = timetable.run()
    if ok:
        rows = load_timetable_rows()
        log_timetable_history(
            semester=semester,
            generated_by=session.get("email", "admin"),
            rows=rows
        )
        return redirect("/admin/dashboard?message=Timetable+generated+successfully.")
    return redirect("/admin/dashboard?error=" + msg.replace(" ", "+"))


@app.route("/admin/timetable/delete")
def delete_timetable_entry():

    if session.get("role") != "admin":
        return "Unauthorized"

    day = request.args.get("day", "")
    slot = request.args.get("slot", "")
    subject = request.args.get("subject", "")
    room = request.args.get("room", "")
    teacher = request.args.get("teacher", "")
    target = request.args.get("target", "")
    section = request.args.get("section", "").strip()

    apply_timetable_delete(day, slot, subject, room, teacher, target)

    redirect_url = "/admin/dashboard?message=Timetable+entry+deleted."
    if section:
        redirect_url += "&section=" + section
    return redirect(redirect_url)


@app.route("/admin/timetable/delete_api", methods=["POST"])
def delete_timetable_entry_api():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    day = request.form.get("day", "").strip()
    slot = request.form.get("slot", "").strip()
    subject = request.form.get("subject", "").strip()
    room = request.form.get("room", "").strip()
    teacher = request.form.get("teacher", "").strip()
    target = request.form.get("target", "").strip()

    deleted = apply_timetable_delete(day, slot, subject, room, teacher, target)
    if not deleted:
        return jsonify({"ok": False, "error": "Entry not found"}), 404
    return jsonify({"ok": True})


@app.route("/admin/timetable/label_absent")
def label_timetable_absent():

    if session.get("role") != "admin":
        return "Unauthorized"

    day = request.args.get("day", "")
    slot = request.args.get("slot", "")
    subject = request.args.get("subject", "")
    room = request.args.get("room", "")
    teacher = request.args.get("teacher", "")
    target = request.args.get("target", "")
    clear = request.args.get("clear", "0") == "1"

    rows = load_timetable_rows()
    updated = False
    for row in rows:
        match = (
            row["day"] == day
            and row["slot"] == slot
            and row["subject"] == subject
            and row["room"] == room
            and row.get("teacher", "") == teacher
            and row.get("target", "ALL") == target
        )
        if match and not updated:
            row["label"] = "" if clear else "Teacher Absent"
            updated = True

    save_timetable_rows(rows)
    if clear:
        return redirect("/admin/dashboard?message=Timetable+label+cleared.")
    return redirect("/admin/dashboard?message=Absent+label+added.")


@app.route("/admin/timetable/edit", methods=["GET", "POST"])
def edit_timetable_entry():

    if session.get("role") != "admin":
        return "Unauthorized"

    old_day = request.values.get("old_day", "").strip()
    old_slot = request.values.get("old_slot", "").strip()
    old_subject = request.values.get("old_subject", "").strip()
    old_room = request.values.get("old_room", "").strip()
    old_teacher = request.values.get("old_teacher", "").strip()
    old_target = request.values.get("old_target", "").strip()
    source_section = request.values.get("source_section", "").strip() or "generate-section"

    rows = load_timetable_rows()
    target_row = None
    for i, row in enumerate(rows):
        if (
            row.get("day", "") == old_day
            and row.get("slot", "") == old_slot
            and row.get("subject", "") == old_subject
            and row.get("room", "") == old_room
            and row.get("teacher", "") == old_teacher
            and row.get("target", "ALL") == old_target
        ):
            target_row = row
            break

    if target_row is None:
        return redirect("/admin/dashboard?error=Timetable+entry+not+found.&section=" + source_section)

    if request.method == "POST":
        new_day = request.form.get("day", target_row.get("day", "")).strip()
        new_slot = request.form.get("slot", target_row.get("slot", "")).strip()
        new_subject = request.form.get("subject", target_row.get("subject", "")).strip()
        new_teacher = request.form.get("teacher", target_row.get("teacher", "")).strip()
        new_room = request.form.get("room", target_row.get("room", "")).strip()
        new_target = request.form.get("target", target_row.get("target", "ALL")).strip() or "ALL"
        new_label = request.form.get("label", target_row.get("label", "")).strip()

        new_row = {
            "day": new_day,
            "slot": new_slot,
            "subject": new_subject,
            "teacher": new_teacher,
            "room": new_room,
            "target": new_target,
            "label": new_label
        }
        updated = apply_timetable_update(
            {
                "day": old_day,
                "slot": old_slot,
                "subject": old_subject,
                "room": old_room,
                "teacher": old_teacher,
                "target": old_target or "ALL"
            },
            new_row
        )
        if not updated:
            return redirect("/admin/dashboard?error=Timetable+entry+not+found.&section=" + source_section)

        return redirect("/admin/dashboard?message=Timetable+entry+updated.&section=" + source_section)

    return render_template(
        "admin_edit_timetable.html",
        row=target_row,
        old_day=old_day,
        old_slot=old_slot,
        old_subject=old_subject,
        old_room=old_room,
        old_teacher=old_teacher,
        old_target=old_target,
        source_section=source_section
    )


@app.route("/admin/timetable/update_api", methods=["POST"])
def update_timetable_entry_api():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    old_row = {
        "day": request.form.get("old_day", "").strip(),
        "slot": request.form.get("old_slot", "").strip(),
        "subject": request.form.get("old_subject", "").strip(),
        "room": request.form.get("old_room", "").strip(),
        "teacher": request.form.get("old_teacher", "").strip(),
        "target": request.form.get("old_target", "").strip() or "ALL"
    }

    new_row = {
        "day": request.form.get("day", old_row["day"]).strip(),
        "slot": request.form.get("slot", old_row["slot"]).strip(),
        "subject": request.form.get("subject", old_row["subject"]).strip(),
        "teacher": request.form.get("teacher", old_row["teacher"]).strip(),
        "room": request.form.get("room", old_row["room"]).strip(),
        "target": request.form.get("target", old_row["target"]).strip() or "ALL",
        "label": request.form.get("label", "").strip()
    }

    updated = apply_timetable_update(old_row, new_row)
    if not updated:
        return jsonify({"ok": False, "error": "Entry not found"}), 404
    return jsonify({"ok": True, "row": new_row})


@app.route("/admin/timetable/teacher_absent")
def mark_teacher_all_absent():

    if session.get("role") != "admin":
        return "Unauthorized"

    teacher = request.args.get("teacher", "")
    clear = request.args.get("clear", "0") == "1"
    rows = load_timetable_rows()
    updated = 0

    for row in rows:
        if row.get("teacher", "") == teacher:
            if clear:
                if row.get("label", "") == "Teacher Absent":
                    row["label"] = ""
                    updated += 1
            else:
                if row.get("label", "") != "Teacher Absent":
                    row["label"] = "Teacher Absent"
                    updated += 1

    save_timetable_rows(rows)
    if clear:
        return redirect("/admin/dashboard?message=Teacher+absence+cleared+for+all+classes.")
    return redirect("/admin/dashboard?message=Teacher+marked+absent+for+all+classes.")


@app.route("/events")
def events():
    role = session.get("role")
    email = session.get("email", "")
    if role not in ("admin", "teacher", "student"):
        return jsonify([])

    all_events = load_events()
    payload = [to_calendar_event(e, email) for e in all_events]
    return jsonify(payload)


@app.route("/teacher/add_event", methods=["POST"])
def add_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"

    if not title or not date:
        return "Missing title/date", 400
    if event_type not in ("general", "test", "exam"):
        event_type = "general"

    all_events = load_events()
    all_events.append({
        "id": str(uuid.uuid4()),
        "title": title,
        "subject": subject if subject else title,
        "date": date,
        "type": event_type,
        "important": important,
        "creator_name": session.get("name", ""),
        "creator_email": session.get("email", ""),
        "creator_role": "teacher"
    })
    save_events(all_events)
    return "OK", 200


@app.route("/teacher/update_event", methods=["POST"])
def update_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"

    all_events = load_events()
    updated = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            if title:
                e["title"] = title
            if subject:
                e["subject"] = subject
            if date:
                e["date"] = date
            if event_type in ("general", "test", "exam"):
                e["type"] = event_type
            e["important"] = important
            updated = True
            break

    if not updated:
        return "Event not found", 404

    save_events(all_events)
    return "OK", 200


@app.route("/teacher/delete_event", methods=["POST"])
def delete_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    all_events = load_events()
    kept = []
    deleted = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            deleted = True
            continue
        kept.append(e)

    if not deleted:
        return "Event not found", 404

    save_events(kept)
    return "OK", 200


@app.route("/admin/events/add", methods=["POST"])
def admin_add_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    if not title or not date:
        return redirect("/admin/dashboard?error=Event+title+and+date+are+required.")
    if event_type not in ("general", "test", "exam", "vacation"):
        event_type = "general"

    all_events = load_events()
    all_events.append({
        "id": str(uuid.uuid4()),
        "title": title,
        "subject": subject if subject else title,
        "date": date,
        "type": event_type,
        "important": important,
        "creator_name": session.get("name", ""),
        "creator_email": session.get("email", ""),
        "creator_role": "admin"
    })
    save_events(all_events)
    return redirect("/admin/dashboard?message=Event+added+successfully.")


@app.route("/admin/events/delete")
def admin_delete_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    event_id = request.args.get("id", "")
    all_events = load_events()
    kept = [e for e in all_events if e.get("id") != event_id]
    save_events(kept)
    return redirect("/admin/dashboard?message=Event+deleted.")


@app.route("/admin/events/edit", methods=["GET", "POST"])
def admin_edit_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    event_id = request.args.get("id", "")
    all_events = load_events()
    target = None
    for e in all_events:
        if e.get("id") == event_id:
            target = e
            break

    if target is None:
        return redirect("/admin/dashboard?error=Event+not+found.")

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "").strip()
        date = request.form.get("date", "").strip()
        event_type = request.form.get("event_type", "general").strip().lower()
        important = request.form.get("important", "no").strip().lower() == "yes"
        if title:
            target["title"] = title
        if subject:
            target["subject"] = subject
        if date:
            target["date"] = date
        if event_type in ("general", "test", "exam", "vacation"):
            target["type"] = event_type
        target["important"] = important
        save_events(all_events)
        return redirect("/admin/dashboard?message=Event+updated.")

    return render_template("admin_edit_event.html", event=target)


# =====================================================
# TEACHER DASHBOARD
# =====================================================
@app.route("/teacher/dashboard")
def teacher_dashboard():

    if session.get("role") != "teacher":
        return redirect("/login")

    teacher = session["name"]
    rows = []
    pending_rows = []
    institute_timetable = load_timetable_rows()
    my_timetable = []
    today_short = datetime.now().strftime("%a")
    today_name = datetime.now().strftime("%A")
    today_classes = []

    for course in load_courses():
        if course["teacher"] == teacher:
            rows.append(course)

    for req in load_preference_requests():
        if req["teacher"] == teacher:
            pending_rows.append(req)

    for row in institute_timetable:
        if row["teacher"] == teacher:
            my_timetable.append(row)
            if row["day"] == today_short:
                today_classes.append(row)

    today_classes.sort(key=lambda r: r["slot"])

    return render_template(
        "teacher_dashboard.html",
        teacher=teacher,
        teacher_email=session.get("email", ""),
        teacher_department=session.get("department", "ALL"),
        teacher_profile_pic=session.get("profile_pic", ""),
        rows=rows,
        pending_rows=pending_rows,
        institute_timetable=institute_timetable,
        my_timetable=my_timetable,
        vacations=get_upcoming_vacations(),
        today_name=today_name,
        today_classes=today_classes
    )


# =====================================================
# TEACHER SUBMIT COURSE
# =====================================================
@app.route("/submit_teacher", methods=["POST"])
def submit_teacher():

    if session.get("role") != "teacher":
        return "Unauthorized"

    subject = request.form["subject"]
    teacher = session["name"]
    students = request.form["students"]
    target = request.form.get("target", "ALL").strip() or "ALL"
    try:
        if int(students) > MAX_ROOM_CAPACITY:
            return "Students count exceeds max room capacity (50). Please split into multiple batches."
    except ValueError:
        return "Invalid students count."

    day1 = request.form["day1"]
    slot1 = request.form["slot1"]
    day2 = request.form["day2"]
    slot2 = request.form["slot2"]
    day3 = request.form["day3"]
    slot3 = request.form["slot3"]

    request_id = f"{teacher}|{subject}".lower()
    new_request = {
        "id": request_id,
        "subject": subject,
        "teacher": teacher,
        "students": students,
        "target": target,
        "prefs": [f"{day1}:{slot1}", f"{day2}:{slot2}", f"{day3}:{slot3}"]
    }

    requests = load_preference_requests()
    updated = False
    for i, req in enumerate(requests):
        if req["id"] == request_id:
            requests[i] = new_request
            updated = True
            break

    if not updated:
        requests.append(new_request)

    save_preference_requests(requests)

    return redirect("/teacher/dashboard")


# =====================================================
# STUDENT DASHBOARD
# =====================================================
@app.route("/student/dashboard")
def student_dashboard():

    if session.get("role") != "student":
        return redirect("/login")

    student_name = session.get("name", "Student")
    student_department = session.get("department", "ALL")
    if not student_department or student_department.upper() == "ALL":
        student_department = infer_department_from_email(session.get("email", ""))

    institute_timetable = load_timetable_rows()
    my_timetable = []
    institute_today = []
    my_today = []
    today_short = datetime.now().strftime("%a")
    today_name = datetime.now().strftime("%A")
    day_order = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5}
    slot_order = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}

    for row in institute_timetable:
        if row["day"] == today_short:
            institute_today.append(row)

    for row in institute_timetable:
        target = row["target"].strip().upper()
        dept = student_department.strip().upper()
        if target == "ALL" or dept == "ALL" or target == dept:
            my_timetable.append(row)
            if row["day"] == today_short:
                my_today.append(row)

    my_timetable.sort(
        key=lambda r: (
            day_order.get(r["day"], 99),
            slot_order.get(r["slot"], 99),
            r["subject"]
        )
    )
    my_today.sort(key=lambda r: (slot_order.get(r["slot"], 99), r["subject"]))
    institute_today.sort(key=lambda r: (slot_order.get(r["slot"], 99), r["subject"]))

    return render_template(
        "student_dashboard.html",
        student_name=student_name,
        student_email=session.get("email", ""),
        student_department=student_department,
        student_profile_pic=session.get("profile_pic", ""),
        institute_timetable=institute_timetable,
        my_timetable=my_timetable,
        my_today=my_today,
        institute_today=institute_today,
        vacations=get_upcoming_vacations(),
        today_name=today_name
    )


@app.route("/student/timetable")
def student_timetable():

    if session.get("role") != "student":
        return redirect("/login")

    timetable_data = []

    if os.path.exists(TIMETABLE_FILE):
        with open(TIMETABLE_FILE) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 4:
                    continue
                day, slot, subject, room = parts[0], parts[1], parts[2], parts[3]
                timetable_data.append({
                    "day": day,
                    "slot": slot,
                    "subject": subject,
                    "room": room
                })

    return render_template(
        "student_timetable.html",
        timetable=timetable_data
    )


@app.route("/profile")
def profile_page():
    role = session.get("role")
    email = session.get("email")
    if not role or not email:
        return redirect("/login")

    users = load_users()
    current = None
    for u in users:
        if u["email"] == email and u["role"] == role:
            current = u
            break

    if current is None:
        return redirect("/login")

    return render_template(
        "profile.html",
        role=role,
        name=current.get("name", ""),
        email=current.get("email", ""),
        department=current.get("department", "ALL"),
        profile_pic=current.get("profile_pic", ""),
        message=request.args.get("message", ""),
        error=request.args.get("error", "")
    )


@app.route("/profile/update", methods=["POST"])
def update_profile():

    role = session.get("role")
    email = session.get("email")
    if not role or not email:
        return redirect("/login")

    users = load_users()
    target = None
    for u in users:
        if u["email"] == email and u["role"] == role:
            target = u
            break

    if target is None:
        return redirect("/login")

    name = request.form.get("name", "").strip()
    department = request.form.get("department", "").strip() or target.get("department", "ALL")
    if role == "admin":
        department = "ALL"

    if name:
        target["name"] = name
        session["name"] = name

    target["department"] = department
    session["department"] = department

    file = request.files.get("profile_pic")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in ALLOWED_IMAGE_EXT:
            os.makedirs(PROFILE_UPLOAD_DIR, exist_ok=True)
            safe_email = secure_filename(email.replace("@", "_at_"))
            filename = f"{role}_{safe_email}_{int(datetime.now().timestamp())}{ext}"
            file_path = os.path.join(PROFILE_UPLOAD_DIR, filename)
            file.save(file_path)
            rel_path = f"profile_pics/{filename}"
            target["profile_pic"] = rel_path
            session["profile_pic"] = rel_path

    save_users(users)
    return redirect("/profile?message=Profile+updated+successfully.")


# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting SmartTimetable on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
