"""Auto-Scheduler backend (Flask + pg8000, pure Python).   Run:  python app.py
Env (all optional): DATABASE_URL, JWT_SECRET, AGENT_URL (friend's agent; unset = mock agent)"""
import json, os, re, threading, time, uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import bcrypt, jwt, pg8000.dbapi, requests
from flask import Flask, Response, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-before-demo")
AGENT_URL = os.getenv("AGENT_URL")
TZ = timezone(timedelta(hours=5, minutes=30))          # IST; no tz database needed
BASE = Path(__file__).parent
UPLOADS = Path(os.getenv("UPLOAD_DIR", BASE / "uploads"))
MAX_BYTES = 5 * 1024 * 1024
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"\+?[0-9]{10,15}")

for sub in ("jd", "resumes"):
    (UPLOADS / sub).mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0      # always reload css/js while developing


# ---------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def conv(o):
    if isinstance(o, (date, datetime, dtime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


def out(data, status=200):
    return Response(json.dumps(data, default=conv), status, mimetype="application/json")


@app.errorhandler(ApiError)
def on_api_error(e):
    return out({"detail": e.msg}, e.status)


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):                     # 404, 405 ... keep their own status
        return out({"detail": e.description}, e.code)
    app.logger.exception(e)
    return out({"detail": "Something went wrong on the server"}, 500)


@app.after_request
def cors(resp):
    """Lets the page work even if it is opened from a different origin (file://, Live Server)."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    return resp


class Tx:
    """One DB transaction per `with` block: commit on success, rollback on error."""
    def __enter__(self):
        u = urlparse(DB_URL)
        self.conn = pg8000.dbapi.connect(user=u.username, password=u.password, host=u.hostname,
                                         port=u.port or 5432, database=u.path.lstrip("/"))
        self.cur = self.conn.cursor()
        return self

    def __exit__(self, et, ev, tb):
        try:
            self.conn.rollback() if et else self.conn.commit()
        finally:
            self.conn.close()

    def all(self, sql, params=()):
        self.cur.execute(sql, params)
        if self.cur.description is None:
            return []
        names = [d[0] for d in self.cur.description]
        return [dict(zip(names, r)) for r in self.cur.fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def run(self, sql, params=()):
        self.cur.execute(sql, params)


def is_unique_violation(e):
    return "23505" in str(e)


# ---------------------------------------------------------------- auth
def make_token(role, uid, cid=None):
    exp = datetime.now(TZ) + timedelta(hours=8)
    return jwt.encode({"role": role, "uid": uid, "cid": cid, "exp": exp}, JWT_SECRET, algorithm="HS256")


def auth(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            h = request.headers.get("Authorization", "")
            if not h.startswith("Bearer "):
                raise ApiError(401, "Please log in")
            try:
                u = jwt.decode(h[7:], JWT_SECRET, algorithms=["HS256"])
            except jwt.PyJWTError:
                raise ApiError(401, "Session expired, please log in again")
            if role and u["role"] != role:
                raise ApiError(403, "Not allowed")
            return fn(u, *a, **kw)
        return wrapper
    return deco


def check_pw(plain, hashed):
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def body():
    return request.get_json(silent=True) or {}


@app.post("/api/auth/candidate/register")
def register():
    b = body()
    username, email, pw = str(b.get("username", "")), str(b.get("email", "")).strip(), str(b.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_]{3,50}", username):
        raise ApiError(422, "Username must be 3-50 letters, digits or underscores")
    if not EMAIL_RE.fullmatch(email):
        raise ApiError(422, "Enter a valid email address")
    if not 8 <= len(pw.encode()) <= 72:
        raise ApiError(422, "Password must be 8-72 characters")
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    try:
        with Tx() as t:
            cid = t.one("INSERT INTO candidates (username, email, password_hash) VALUES (%s,%s,%s) "
                        "RETURNING candidate_id", (username, email.lower(), h))["candidate_id"]
    except pg8000.dbapi.DatabaseError as e:
        if is_unique_violation(e):
            raise ApiError(409, "That username or email is already registered. Log in instead")
        raise
    return out({"token": make_token("candidate", cid), "role": "candidate", "name": username})


@app.post("/api/auth/candidate/login")
def candidate_login():
    b = body()
    with Tx() as t:
        r = t.one("SELECT candidate_id, password_hash FROM candidates WHERE username=%s",
                  (str(b.get("username", "")),))
    if not r or not check_pw(str(b.get("password", "")), r["password_hash"]):
        raise ApiError(401, "Wrong username or password")
    return out({"token": make_token("candidate", r["candidate_id"]), "role": "candidate", "name": b["username"]})


@app.post("/api/auth/hr/login")
def hr_login():
    b = body()
    with Tx() as t:
        r = t.one("SELECT hr_id, password_hash, company_id, company_name FROM hr_users WHERE login_id=%s",
                  (str(b.get("login_id", "")),))
    if not r or not check_pw(str(b.get("password", "")), r["password_hash"]):
        raise ApiError(401, "Wrong login ID or password")
    return out({"token": make_token("hr", r["hr_id"], r["company_id"]), "role": "hr",
                "name": b["login_id"], "company_name": r["company_name"]})


# ---------------------------------------------------------------- files / jobs
def read_upload(field, allowed=(".pdf", ".docx")):
    nice = " or ".join(e[1:].upper() for e in allowed)
    f = request.files.get(field)
    if not f or not f.filename:
        raise ApiError(422, "Upload a %s file" % nice)
    ext = Path(f.filename).suffix.lower()
    if ext not in allowed:
        raise ApiError(422, "Upload a %s file" % nice)
    data = f.read(MAX_BYTES + 1)
    if not data:
        raise ApiError(422, "The uploaded file is empty")
    if len(data) > MAX_BYTES:
        raise ApiError(413, "File is larger than 5 MB")
    return data, ext, f.filename


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_jd(raw):
    """Check a JD file against the agreed JSON format. Returns (jd_dict, None) or (None, error)."""
    try:
        jd = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None, "The JD file is not valid JSON"
    if not isinstance(jd, dict):
        return None, "The JD file must contain a JSON object"
    if not isinstance(jd.get("job_title"), str) or not jd["job_title"].strip():
        return None, "JD: job_title must be a non-empty text"
    if not is_num(jd.get("minimum_experience_years")) or jd["minimum_experience_years"] < 0:
        return None, "JD: minimum_experience_years must be a number, 0 or more"
    for key, required in (("required_skills", True), ("preferred_skills", False)):
        v = jd.get(key, None if required else [])
        if not isinstance(v, list) or (required and not v) or not all(isinstance(x, str) and x.strip() for x in v):
            return None, "JD: %s must be a list of skill names%s" % (key, " (at least one)" if required else "")
    w = jd.get("weights")
    if w is not None and not (isinstance(w, dict) and is_num(w.get("skills")) and is_num(w.get("experience"))
                              and w["skills"] >= 0 and w["experience"] >= 0):
        return None, "JD: weights must have numeric skills and experience"
    n = jd.get("top_n")
    if n is not None and not (isinstance(n, int) and not isinstance(n, bool) and n > 0):
        return None, "JD: top_n must be a whole number above 0"
    return jd, None


def save_file(data, ext, folder):
    rel = "%s/%s%s" % (folder, uuid.uuid4().hex, ext)
    (UPLOADS / rel).write_bytes(data)
    return rel


def get_job(t, job_id):
    j = t.one("SELECT *, deadline > now() AS is_open FROM job_postings WHERE job_id=%s", (job_id,))
    if not j:
        raise ApiError(404, "Job not found")
    return j


def open_dates(t, job):
    """Dates a candidate may pick: inside HR's window AND with a free panel slot."""
    rows = t.all("""SELECT DISTINCT slot_date FROM panel_availability
                    WHERE company_id=%s AND is_available
                      AND slot_date BETWEEN %s AND %s AND slot_date >= CURRENT_DATE
                      AND start_time >= %s AND end_time <= %s
                    ORDER BY slot_date""",
                 (job["company_id"], job["interview_start_date"], job["interview_end_date"],
                  job["daily_start_time"], job["daily_end_time"]))
    return [r["slot_date"] for r in rows]


def parse_date(s):
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise ApiError(422, "Check the date fields")


@app.get("/api/jobs")
@auth("candidate")
def list_jobs(u):
    role = request.args.get("role") or None
    with Tx() as t:
        return out(t.all("""SELECT jp.job_id, jp.company_name, jp.job_title, jp.deadline,
                                   jp.deadline > now() AS is_open,
                                   substring(jp.jd_path from '\\.[^.]+$') AS jd_ext,
                                   EXISTS (SELECT 1 FROM applications a
                                           WHERE a.job_id=jp.job_id AND a.candidate_id=%s) AS applied
                            FROM job_postings jp
                            WHERE (%s::text IS NULL OR jp.job_title=%s)
                            ORDER BY is_open DESC, jp.deadline""", (u["uid"], role, role)))


@app.get("/api/jobs/<int:job_id>/dates")
@auth("candidate")
def job_dates(u, job_id):
    with Tx() as t:
        return out(open_dates(t, get_job(t, job_id)))


@app.get("/api/files/jd/<int:job_id>")
@auth()
def jd_file(u, job_id):
    with Tx() as t:
        j = get_job(t, job_id)
    p = UPLOADS / j["jd_path"]
    return send_file(p, as_attachment=True, download_name=p.name)


@app.post("/api/jobs/<int:job_id>/apply")
@auth("candidate")
def apply(u, job_id):
    f = request.form
    name, phone, mail = f.get("full_name", "").strip(), f.get("contact_number", "").strip(), f.get("contact_email", "").strip()
    if not name:
        raise ApiError(422, "Name is required")
    if not PHONE_RE.fullmatch(phone):
        raise ApiError(422, "Enter a contact number with 10-15 digits")
    if not EMAIL_RE.fullmatch(mail):
        raise ApiError(422, "Enter a valid contact email")
    prefs = [parse_date(f.get("pref1")), parse_date(f.get("pref2")), parse_date(f.get("pref3"))]
    data, ext, orig = read_upload("resume")
    path = None
    try:
        with Tx() as t:
            job = get_job(t, job_id)
            if not job["is_open"]:
                raise ApiError(403, "Application closed")
            ok = set(open_dates(t, job))
            if len(set(prefs)) != 3 or any(p not in ok for p in prefs):
                raise ApiError(422, "Choose three different dates from the list")
            path = save_file(data, ext, "resumes")
            app_id = t.one("""INSERT INTO applications (candidate_id, job_id, company_id, role, full_name,
                                contact_number, contact_email, resume_path, resume_filename,
                                pref_date_1, pref_date_2, pref_date_3)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING application_id""",
                           (u["uid"], job_id, job["company_id"], job["job_title"], name, phone, mail,
                            path, orig[:255], prefs[0], prefs[1], prefs[2]))["application_id"]
    except pg8000.dbapi.DatabaseError as e:
        if path:
            (UPLOADS / path).unlink(missing_ok=True)
        if is_unique_violation(e):
            raise ApiError(409, "You have already applied for this job")
        raise
    except ApiError:
        if path:
            (UPLOADS / path).unlink(missing_ok=True)
        raise
    threading.Thread(target=process_application, args=(app_id,), daemon=True).start()
    return out({"application_id": app_id, "status": "IN_PROGRESS"})


@app.get("/api/my/applications")
@auth("candidate")
def my_applications(u):
    with Tx() as t:
        return out(t.all("""SELECT a.application_id, a.role, jp.company_name, a.status::text AS status, a.applied_at,
                                   i.interview_date, i.start_time, i.end_time, i.preference_rank
                            FROM applications a
                            JOIN job_postings jp ON jp.job_id = a.job_id
                            LEFT JOIN interviews i ON i.application_id = a.application_id
                            WHERE a.candidate_id=%s ORDER BY a.applied_at DESC""", (u["uid"],)))


# ---------------------------------------------------------------- HR
@app.post("/api/hr/jobs")
@auth("hr")
def create_job(u):
    f = request.form
    title = f.get("job_title", "").strip()
    try:
        dl = datetime.fromisoformat(f.get("deadline", ""))
        t1, t2 = dtime.fromisoformat(f.get("daily_start_time", "")), dtime.fromisoformat(f.get("daily_end_time", ""))
    except ValueError:
        raise ApiError(422, "Check the deadline and time fields")
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=TZ)
    d1, d2 = parse_date(f.get("interview_start_date")), parse_date(f.get("interview_end_date"))
    if not title:
        raise ApiError(422, "Job title is required")
    if dl <= datetime.now(TZ):
        raise ApiError(422, "Deadline must be in the future")
    if d1 <= dl.astimezone(TZ).date():
        raise ApiError(422, "Interviews must start after the application deadline")
    if d2 < d1:
        raise ApiError(422, "Interview end date is before the start date")
    if t2 <= t1:
        raise ApiError(422, "Daily end time must be after the start time")
    data, ext, _ = read_upload("jd", (".json",))
    jd, err = validate_jd(data)
    if err:
        raise ApiError(422, err)
    if jd["job_title"].strip().lower() != title.lower():
        raise ApiError(422, "Job title in the form (%s) and in the JD file (%s) must match" % (title, jd["job_title"].strip()))
    path = save_file(data, ext, "jd")
    with Tx() as t:
        hr = t.one("SELECT company_id, company_name FROM hr_users WHERE hr_id=%s", (u["uid"],))
        jid = t.one("""INSERT INTO job_postings (hr_id, company_id, company_name, job_title, jd_path, deadline,
                         interview_start_date, interview_end_date, daily_start_time, daily_end_time)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING job_id""",
                    (u["uid"], hr["company_id"], hr["company_name"], title, path, dl, d1, d2, t1, t2))["job_id"]
    return out({"job_id": jid})


@app.get("/api/hr/jobs")
@auth("hr")
def hr_jobs(u):
    with Tx() as t:
        return out(t.all("""SELECT jp.*, deadline > now() AS is_open,
                                   substring(jd_path from '\\.[^.]+$') AS jd_ext,
                                   (SELECT count(*) FROM applications a WHERE a.job_id=jp.job_id) AS applicants
                            FROM job_postings jp WHERE company_id=%s ORDER BY created_at DESC""", (u["cid"],)))


@app.get("/api/hr/applications")
@auth("hr")
def hr_applications(u):
    status = request.args.get("status") or None
    with Tx() as t:
        return out(t.all("""SELECT a.application_id, a.full_name, a.contact_email, a.contact_number, a.role,
                                   a.status::text AS status, a.agent_decision, a.status_reason, a.applied_at,
                                   a.resume_filename, a.pref_date_1, a.pref_date_2, a.pref_date_3,
                                   i.interview_date, i.start_time, i.end_time, i.preference_rank,
                                   pm.name AS panelist
                            FROM applications a
                            JOIN job_postings jp ON jp.job_id = a.job_id
                            LEFT JOIN interviews i ON i.application_id = a.application_id
                            LEFT JOIN panel_members pm ON pm.person_id = i.person_id
                            WHERE jp.company_id=%s AND (%s::text IS NULL OR a.status::text=%s)
                            ORDER BY a.applied_at DESC""", (u["cid"], status, status)))


@app.patch("/api/hr/applications/<int:app_id>")
@auth("hr")
def hr_decide(u, app_id):
    """HR resolves a REFERRED_TO_HR case: ACCEPT books a slot, REJECT closes it."""
    d = str(body().get("decision", "")).upper()
    if d not in ("ACCEPT", "REJECT"):
        raise ApiError(422, "Decision must be ACCEPT or REJECT")
    with Tx() as t:
        a = load_app(t, app_id, lock=True)
        if not a or a["job_company_id"] != u["cid"]:
            raise ApiError(404, "Application not found")
        if a["status"] != "REFERRED_TO_HR":
            raise ApiError(409, "This application is not waiting for HR review")
        if d == "REJECT":
            set_status(t, app_id, "REJECTED", None, "Rejected by HR after manual review")
        else:
            if not try_schedule(t, a):
                raise ApiError(409, "No free panel slot is left in the interview window")
            set_status(t, app_id, "INTERVIEW_SCHEDULED", None, "Approved by HR after manual review")
    return out({"ok": True})


@app.get("/api/hr/applications/<int:app_id>/resume")
@auth("hr")
def hr_resume(u, app_id):
    with Tx() as t:
        a = load_app(t, app_id)
    if not a or a["job_company_id"] != u["cid"]:
        raise ApiError(404, "Application not found")
    return send_file(UPLOADS / a["resume_path"], as_attachment=True, download_name=a["resume_filename"])


# ---------------------------------------------------------------- agent
def call_agent(jd_rel, resume_rel, resume_name, app_id):
    """Contract with the agent: POST multipart (jd, resume, application_id)
    -> {"decision": "ACCEPT"|"REJECT"|"UNCERTAIN", "reason": "..."}"""
    if not AGENT_URL:                                   # MOCK until the real agent is ready
        n = resume_name.lower()
        if "reject" in n:
            return "REJECT", "mock agent"
        if "uncertain" in n:
            return "UNCERTAIN", "mock agent"
        return "ACCEPT", "mock agent"
    with open(UPLOADS / jd_rel, "rb") as jd, open(UPLOADS / resume_rel, "rb") as rs:
        r = requests.post(AGENT_URL, data={"application_id": str(app_id)},
                          files={"jd": jd, "resume": rs}, timeout=120)
    r.raise_for_status()
    j = r.json()
    d = str(j["decision"]).upper()
    if d not in ("ACCEPT", "REJECT", "UNCERTAIN"):
        raise ValueError("bad decision %r" % d)
    return d, j.get("reason")


def process_application(app_id):
    """Runs in a background thread after the candidate's request has returned."""
    try:
        with Tx() as t:
            a = load_app(t, app_id)
        decision, reason = None, None
        for attempt in range(3):                        # retry the agent, then fall back to HR
            try:
                decision, reason = call_agent(a["jd_path"], a["resume_path"], a["resume_filename"], app_id)
                break
            except Exception:
                time.sleep(1 + attempt)
        if decision is None:
            decision, reason = "UNCERTAIN", "Agent error: could not get a decision"
        with Tx() as t:
            a = load_app(t, app_id, lock=True)
            if a["status"] != "IN_PROGRESS":            # already handled
                return
            if decision == "REJECT":
                set_status(t, app_id, "REJECTED", decision, reason)
            elif decision == "ACCEPT":
                if try_schedule(t, a):
                    set_status(t, app_id, "INTERVIEW_SCHEDULED", decision, reason)
                else:
                    set_status(t, app_id, "REFERRED_TO_HR", decision, "No free interview slot")
            else:
                set_status(t, app_id, "REFERRED_TO_HR", decision, reason)
    except Exception:
        app.logger.exception("process_application failed for %s", app_id)


# ---------------------------------------------------------------- scheduler
def load_app(t, app_id, lock=False):
    return t.one("""SELECT a.*, a.status::text AS status, jp.jd_path, jp.interview_start_date,
                           jp.interview_end_date, jp.daily_start_time, jp.daily_end_time,
                           jp.company_id AS job_company_id
                    FROM applications a JOIN job_postings jp ON jp.job_id = a.job_id
                    WHERE a.application_id=%s""" + (" FOR UPDATE OF a" if lock else ""), (app_id,))


def set_status(t, app_id, status, decision=None, reason=None):
    t.run("""UPDATE applications SET status=%s::app_status,
             agent_decision=COALESCE(%s::varchar, agent_decision),
             status_reason=%s::text, updated_at=now() WHERE application_id=%s""",
          (status, decision, reason, app_id))


# Earliest free slot (optionally on one date) in the job's window; ties go to the least-loaded
# panelist. SKIP LOCKED means two concurrent workers can never grab the same slot.
SLOT_SQL = """
SELECT availability_id, person_id, slot_date, start_time, end_time
FROM panel_availability
WHERE company_id=%s AND is_available
  AND slot_date BETWEEN %s AND %s AND slot_date >= CURRENT_DATE
  AND (%s::date IS NULL OR slot_date = %s::date)
  AND start_time >= %s AND end_time <= %s
ORDER BY slot_date, start_time,
         (SELECT count(*) FROM interviews i WHERE i.person_id = panel_availability.person_id)
LIMIT 1
FOR UPDATE OF panel_availability SKIP LOCKED"""


def try_schedule(t, a):
    """Book a slot: preference 1, then 2, then 3, then the earliest free slot in the window.
    Runs inside the caller's transaction, so booking and status change commit together."""
    attempts = [(1, a["pref_date_1"]), (2, a["pref_date_2"]), (3, a["pref_date_3"]), (None, None)]
    for rank, day in attempts:
        s = t.one(SLOT_SQL, (a["company_id"], a["interview_start_date"], a["interview_end_date"],
                             day, day, a["daily_start_time"], a["daily_end_time"]))
        if s:
            t.run("UPDATE panel_availability SET is_available=FALSE WHERE availability_id=%s",
                  (s["availability_id"],))
            t.run("""INSERT INTO interviews (application_id, availability_id, person_id,
                       interview_date, start_time, end_time, preference_rank)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                  (a["application_id"], s["availability_id"], s["person_id"],
                   s["slot_date"], s["start_time"], s["end_time"], rank))
            return True
    return False


# ---------------------------------------------------------------- frontend
@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)