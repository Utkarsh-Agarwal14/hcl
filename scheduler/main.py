"""Auto-Scheduler backend.  Run:  uvicorn main:app --reload
Env: DATABASE_URL, JWT_SECRET, AGENT_URL (friend's agent; unset = mock agent), APP_TZ"""
import os, re, time, uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import bcrypt, httpx, jwt
from psycopg2 import errors, pool
from psycopg2.extras import RealDictCursor
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, UploadFile)
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/recruiter")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-before-demo")
AGENT_URL = os.getenv("AGENT_URL")
TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Kolkata"))
BASE = Path(__file__).parent
UPLOADS = Path(os.getenv("UPLOAD_DIR", BASE / "uploads"))
MAX_BYTES = 5 * 1024 * 1024
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_RE = re.compile(r"\+?[0-9]{10,15}")

for sub in ("jd", "resumes"):
    (UPLOADS / sub).mkdir(parents=True, exist_ok=True)
_sample = UPLOADS / "jd" / "sample_jd.txt"
if not _sample.exists():
    _sample.write_text("Sample job description.\nReplace with a real JD uploaded by HR.\n")

pg = pool.ThreadedConnectionPool(1, 10, DB_URL)
app = FastAPI(title="Auto-Scheduler")


@contextmanager
def db():
    """One transaction per `with` block: commit on success, rollback on error."""
    conn = pg.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pg.putconn(conn)


# ---------------------------------------------------------------- auth
bearer = HTTPBearer(auto_error=False)


def make_token(role, uid, cid=None):
    exp = datetime.now(TZ) + timedelta(hours=8)
    return jwt.encode({"role": role, "uid": uid, "cid": cid, "exp": exp}, JWT_SECRET, algorithm="HS256")


def current(creds=Depends(bearer)):
    if not creds:
        raise HTTPException(401, "Please log in")
    try:
        return jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please log in again")


def need(role):
    def dep(u=Depends(current)):
        if u["role"] != role:
            raise HTTPException(403, "Not allowed")
        return u
    return dep


def check_pw(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


class Register(BaseModel):
    username: str
    email: str
    password: str


class CandLogin(BaseModel):
    username: str
    password: str


class HRLogin(BaseModel):
    login_id: str
    password: str


class Decision(BaseModel):
    decision: str


@app.post("/api/auth/candidate/register")
def register(b: Register):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,50}", b.username):
        raise HTTPException(422, "Username must be 3-50 letters, digits or underscores")
    if not EMAIL_RE.fullmatch(b.email):
        raise HTTPException(422, "Enter a valid email address")
    if not 8 <= len(b.password.encode()) <= 72:
        raise HTTPException(422, "Password must be 8-72 characters")
    h = bcrypt.hashpw(b.password.encode(), bcrypt.gensalt()).decode()
    try:
        with db() as cur:
            cur.execute("INSERT INTO candidates (username, email, password_hash) "
                        "VALUES (%s,%s,%s) RETURNING candidate_id",
                        (b.username, b.email.lower(), h))
            cid = cur.fetchone()["candidate_id"]
    except errors.UniqueViolation:
        raise HTTPException(409, "That username or email is already registered. Log in instead")
    return {"token": make_token("candidate", cid), "role": "candidate", "name": b.username}


@app.post("/api/auth/candidate/login")
def candidate_login(b: CandLogin):
    with db() as cur:
        cur.execute("SELECT candidate_id, password_hash FROM candidates WHERE username=%s", (b.username,))
        r = cur.fetchone()
    if not r or not check_pw(b.password, r["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    return {"token": make_token("candidate", r["candidate_id"]), "role": "candidate", "name": b.username}


@app.post("/api/auth/hr/login")
def hr_login(b: HRLogin):
    with db() as cur:
        cur.execute("SELECT hr_id, password_hash, company_id, company_name FROM hr_users WHERE login_id=%s",
                    (b.login_id,))
        r = cur.fetchone()
    if not r or not check_pw(b.password, r["password_hash"]):
        raise HTTPException(401, "Wrong login ID or password")
    return {"token": make_token("hr", r["hr_id"], r["company_id"]), "role": "hr",
            "name": b.login_id, "company_name": r["company_name"]}


# ---------------------------------------------------------------- files
def read_upload(f: UploadFile):
    ext = Path(f.filename or "").suffix.lower()
    if ext not in (".pdf", ".docx"):
        raise HTTPException(422, "Upload a PDF or DOCX file")
    data = f.file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(422, "The uploaded file is empty")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 5 MB")
    return data, ext


def save_file(data: bytes, ext: str, folder: str) -> str:
    rel = f"{folder}/{uuid.uuid4().hex}{ext}"
    (UPLOADS / rel).write_bytes(data)
    return rel


def get_job(cur, job_id):
    cur.execute("SELECT *, deadline > now() AS is_open FROM job_postings WHERE job_id=%s", (job_id,))
    j = cur.fetchone()
    if not j:
        raise HTTPException(404, "Job not found")
    return j


def open_dates(cur, job):
    """Dates a candidate may pick: inside HR's window AND with a free panel slot."""
    cur.execute("""SELECT DISTINCT slot_date FROM panel_availability
                   WHERE company_id=%s AND is_available
                     AND slot_date BETWEEN %s AND %s AND slot_date >= CURRENT_DATE
                     AND start_time >= %s AND end_time <= %s
                   ORDER BY slot_date""",
                (job["company_id"], job["interview_start_date"], job["interview_end_date"],
                 job["daily_start_time"], job["daily_end_time"]))
    return [r["slot_date"] for r in cur.fetchall()]


# ---------------------------------------------------------------- HR
@app.post("/api/hr/jobs")
def create_job(job_title: str = Form(...), deadline: str = Form(...),
               interview_start_date: date = Form(...), interview_end_date: date = Form(...),
               daily_start_time: str = Form(...), daily_end_time: str = Form(...),
               jd: UploadFile = File(...), u=Depends(need("hr"))):
    try:
        dl = datetime.fromisoformat(deadline)
        t1, t2 = dtime.fromisoformat(daily_start_time), dtime.fromisoformat(daily_end_time)
    except ValueError:
        raise HTTPException(422, "Check the deadline and time fields")
    if dl.tzinfo is None:
        dl = dl.replace(tzinfo=TZ)
    title = job_title.strip()
    if not title:
        raise HTTPException(422, "Job title is required")
    if dl <= datetime.now(TZ):
        raise HTTPException(422, "Deadline must be in the future")
    if interview_start_date <= dl.astimezone(TZ).date():
        raise HTTPException(422, "Interviews must start after the application deadline")
    if interview_end_date < interview_start_date:
        raise HTTPException(422, "Interview end date is before the start date")
    if t2 <= t1:
        raise HTTPException(422, "Daily end time must be after the start time")
    data, ext = read_upload(jd)
    path = save_file(data, ext, "jd")
    with db() as cur:
        cur.execute("SELECT company_id, company_name FROM hr_users WHERE hr_id=%s", (u["uid"],))
        hr = cur.fetchone()
        cur.execute("""INSERT INTO job_postings (hr_id, company_id, company_name, job_title, jd_path,
                         deadline, interview_start_date, interview_end_date, daily_start_time, daily_end_time)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING job_id""",
                    (u["uid"], hr["company_id"], hr["company_name"], title, path, dl,
                     interview_start_date, interview_end_date, t1, t2))
        return {"job_id": cur.fetchone()["job_id"]}


@app.get("/api/hr/jobs")
def hr_jobs(u=Depends(need("hr"))):
    with db() as cur:
        cur.execute("""SELECT jp.*, deadline > now() AS is_open,
                              substring(jd_path from '\\.[^.]+$') AS jd_ext,
                              (SELECT count(*) FROM applications a WHERE a.job_id=jp.job_id) AS applicants
                       FROM job_postings jp WHERE company_id=%s ORDER BY created_at DESC""", (u["cid"],))
        return cur.fetchall()


@app.get("/api/hr/applications")
def hr_applications(status: Optional[str] = None, u=Depends(need("hr"))):
    with db() as cur:
        cur.execute("""SELECT a.application_id, a.full_name, a.contact_email, a.contact_number, a.role,
                              a.status, a.agent_decision, a.status_reason, a.applied_at, a.resume_filename,
                              a.pref_date_1, a.pref_date_2, a.pref_date_3,
                              i.interview_date, i.start_time, i.end_time, i.preference_rank,
                              pm.name AS panelist
                       FROM applications a
                       JOIN job_postings jp ON jp.job_id = a.job_id
                       LEFT JOIN interviews i ON i.application_id = a.application_id
                       LEFT JOIN panel_members pm ON pm.person_id = i.person_id
                       WHERE jp.company_id=%s AND (%s::text IS NULL OR a.status::text=%s)
                       ORDER BY a.applied_at DESC""", (u["cid"], status, status))
        return cur.fetchall()


@app.patch("/api/hr/applications/{app_id}")
def hr_decide(app_id: int, b: Decision, u=Depends(need("hr"))):
    """HR resolves a REFERRED_TO_HR case: ACCEPT books a slot, REJECT closes it."""
    d = b.decision.upper()
    if d not in ("ACCEPT", "REJECT"):
        raise HTTPException(422, "Decision must be ACCEPT or REJECT")
    with db() as cur:
        a = load_app(cur, app_id, lock=True)
        if not a or a["job_company_id"] != u["cid"]:
            raise HTTPException(404, "Application not found")
        if a["status"] != "REFERRED_TO_HR":
            raise HTTPException(409, "This application is not waiting for HR review")
        if d == "REJECT":
            set_status(cur, app_id, "REJECTED", None, "Rejected by HR after manual review")
        else:
            if not try_schedule(cur, a):
                raise HTTPException(409, "No free panel slot is left in the interview window")
            set_status(cur, app_id, "INTERVIEW_SCHEDULED", None, "Approved by HR after manual review")
    return {"ok": True}


@app.get("/api/hr/applications/{app_id}/resume")
def hr_resume(app_id: int, u=Depends(need("hr"))):
    with db() as cur:
        a = load_app(cur, app_id)
    if not a or a["job_company_id"] != u["cid"]:
        raise HTTPException(404, "Application not found")
    return FileResponse(UPLOADS / a["resume_path"], filename=a["resume_filename"])


# ---------------------------------------------------------------- candidate
@app.get("/api/jobs")
def list_jobs(role: Optional[str] = None, u=Depends(need("candidate"))):
    with db() as cur:
        cur.execute("""SELECT jp.job_id, jp.company_name, jp.job_title, jp.deadline,
                              jp.deadline > now() AS is_open,
                              substring(jp.jd_path from '\\.[^.]+$') AS jd_ext,
                              EXISTS (SELECT 1 FROM applications a
                                      WHERE a.job_id=jp.job_id AND a.candidate_id=%s) AS applied
                       FROM job_postings jp
                       WHERE (%s::text IS NULL OR jp.job_title=%s)
                       ORDER BY is_open DESC, jp.deadline""", (u["uid"], role, role))
        return cur.fetchall()


@app.get("/api/jobs/{job_id}/dates")
def job_dates(job_id: int, u=Depends(need("candidate"))):
    with db() as cur:
        return open_dates(cur, get_job(cur, job_id))


@app.get("/api/files/jd/{job_id}")
def jd_file(job_id: int, u=Depends(current)):
    with db() as cur:
        j = get_job(cur, job_id)
    return FileResponse(UPLOADS / j["jd_path"], filename=Path(j["jd_path"]).name)


@app.post("/api/jobs/{job_id}/apply")
def apply(job_id: int, bg: BackgroundTasks,
          full_name: str = Form(...), contact_number: str = Form(...), contact_email: str = Form(...),
          pref1: date = Form(...), pref2: date = Form(...), pref3: date = Form(...),
          resume: UploadFile = File(...), u=Depends(need("candidate"))):
    name, phone, mail = full_name.strip(), contact_number.strip(), contact_email.strip()
    if not name:
        raise HTTPException(422, "Name is required")
    if not PHONE_RE.fullmatch(phone):
        raise HTTPException(422, "Enter a contact number with 10-15 digits")
    if not EMAIL_RE.fullmatch(mail):
        raise HTTPException(422, "Enter a valid contact email")
    data, ext = read_upload(resume)
    prefs = [pref1, pref2, pref3]
    with db() as cur:
        job = get_job(cur, job_id)
        if not job["is_open"]:
            raise HTTPException(403, "Application closed")
        ok = set(open_dates(cur, job))
        if len(set(prefs)) != 3 or any(p not in ok for p in prefs):
            raise HTTPException(422, "Choose three different dates from the list")
        path = save_file(data, ext, "resumes")
        try:
            cur.execute("""INSERT INTO applications (candidate_id, job_id, company_id, role, full_name,
                             contact_number, contact_email, resume_path, resume_filename,
                             pref_date_1, pref_date_2, pref_date_3)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING application_id""",
                        (u["uid"], job_id, job["company_id"], job["job_title"], name, phone, mail,
                         path, resume.filename[:255], *prefs))
            app_id = cur.fetchone()["application_id"]
        except errors.UniqueViolation:
            (UPLOADS / path).unlink(missing_ok=True)
            raise HTTPException(409, "You have already applied for this job")
    bg.add_task(process_application, app_id)   # runs after the response is sent
    return {"application_id": app_id, "status": "IN_PROGRESS"}


@app.get("/api/my/applications")
def my_applications(u=Depends(need("candidate"))):
    with db() as cur:
        cur.execute("""SELECT a.application_id, a.role, jp.company_name, a.status, a.applied_at,
                              i.interview_date, i.start_time, i.end_time, i.preference_rank
                       FROM applications a
                       JOIN job_postings jp ON jp.job_id = a.job_id
                       LEFT JOIN interviews i ON i.application_id = a.application_id
                       WHERE a.candidate_id=%s ORDER BY a.applied_at DESC""", (u["uid"],))
        return cur.fetchall()


# ---------------------------------------------------------------- agent
def call_agent(jd_rel, resume_rel, resume_name, app_id):
    """Contract with the agent: POST multipart (jd, resume, application_id)
    -> {"decision": "ACCEPT"|"REJECT"|"UNCERTAIN", "reason": "..."}"""
    if not AGENT_URL:                              # MOCK, for demos until the real agent is ready
        n = resume_name.lower()
        if "reject" in n:
            return "REJECT", "mock agent"
        if "uncertain" in n:
            return "UNCERTAIN", "mock agent"
        return "ACCEPT", "mock agent"
    with open(UPLOADS / jd_rel, "rb") as jd, open(UPLOADS / resume_rel, "rb") as rs:
        r = httpx.post(AGENT_URL, data={"application_id": str(app_id)},
                       files={"jd": jd, "resume": rs}, timeout=120)
    r.raise_for_status()
    j = r.json()
    d = str(j["decision"]).upper()
    if d not in ("ACCEPT", "REJECT", "UNCERTAIN"):
        raise ValueError(f"bad decision {d!r}")
    return d, j.get("reason")


def process_application(app_id: int):
    with db() as cur:
        a = load_app(cur, app_id)
    decision, reason = None, None
    for attempt in range(3):                       # retry the agent, then fall back to HR
        try:
            decision, reason = call_agent(a["jd_path"], a["resume_path"], a["resume_filename"], app_id)
            break
        except Exception:
            time.sleep(1 + attempt)
    if decision is None:
        decision, reason = "UNCERTAIN", "Agent error: could not get a decision"
    with db() as cur:
        a = load_app(cur, app_id, lock=True)
        if a["status"] != "IN_PROGRESS":           # already handled
            return
        if decision == "REJECT":
            set_status(cur, app_id, "REJECTED", decision, reason)
        elif decision == "ACCEPT":
            if try_schedule(cur, a):
                set_status(cur, app_id, "INTERVIEW_SCHEDULED", decision, reason)
            else:
                set_status(cur, app_id, "REFERRED_TO_HR", decision, "No free interview slot")
        else:
            set_status(cur, app_id, "REFERRED_TO_HR", decision, reason)


# ---------------------------------------------------------------- scheduler
def load_app(cur, app_id, lock=False):
    cur.execute("""SELECT a.*, jp.jd_path, jp.interview_start_date, jp.interview_end_date,
                          jp.daily_start_time, jp.daily_end_time, jp.company_id AS job_company_id
                   FROM applications a JOIN job_postings jp ON jp.job_id = a.job_id
                   WHERE a.application_id=%s""" + (" FOR UPDATE OF a" if lock else ""), (app_id,))
    return cur.fetchone()


def set_status(cur, app_id, status, decision=None, reason=None):
    cur.execute("""UPDATE applications SET status=%s, agent_decision=COALESCE(%s, agent_decision),
                   status_reason=%s, updated_at=now() WHERE application_id=%s""",
                (status, decision, reason, app_id))


# Earliest free slot (optionally on one date) in the job's window; ties go to the least-loaded
# panelist. SKIP LOCKED means two concurrent workers can never grab the same slot.
SLOT_SQL = """
SELECT availability_id, person_id, slot_date, start_time, end_time
FROM panel_availability
WHERE company_id=%(cid)s AND is_available
  AND slot_date BETWEEN %(s)s AND %(e)s AND slot_date >= CURRENT_DATE
  AND (%(d)s::date IS NULL OR slot_date = %(d)s)
  AND start_time >= %(ds)s AND end_time <= %(de)s
ORDER BY slot_date, start_time,
         (SELECT count(*) FROM interviews i WHERE i.person_id = panel_availability.person_id)
LIMIT 1
FOR UPDATE OF panel_availability SKIP LOCKED"""


def try_schedule(cur, a) -> bool:
    """Book a slot: preference 1, then 2, then 3, then the earliest free slot in the window.
    Runs inside the caller's transaction, so booking and status change commit together."""
    attempts = [(1, a["pref_date_1"]), (2, a["pref_date_2"]), (3, a["pref_date_3"]), (None, None)]
    for rank, day in attempts:
        cur.execute(SLOT_SQL, {"cid": a["company_id"], "s": a["interview_start_date"],
                               "e": a["interview_end_date"], "d": day,
                               "ds": a["daily_start_time"], "de": a["daily_end_time"]})
        s = cur.fetchone()
        if s:
            cur.execute("UPDATE panel_availability SET is_available=FALSE WHERE availability_id=%s",
                        (s["availability_id"],))
            cur.execute("""INSERT INTO interviews (application_id, availability_id, person_id,
                             interview_date, start_time, end_time, preference_rank)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (a["application_id"], s["availability_id"], s["person_id"],
                         s["slot_date"], s["start_time"], s["end_time"], rank))
            return True
    return False


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")