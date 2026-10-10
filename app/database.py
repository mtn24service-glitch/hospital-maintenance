"""Database layer for the Hospital Maintenance & Operations complaint system.

Prince Mohammed Bin Nasser Hospital - Jazan.

Engine-agnostic: SQLite locally, PostgreSQL/Neon in the cloud (DATABASE_URL).
Implements the full complaint lifecycle, departments, fault types, specialties,
attachments, audit trail, technicians/assignments and notifications log.
"""

import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime

from . import db as _db

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

ROLES = [
    ("admin", "مدير النظام"),
    ("maint_manager", "مسؤول الصيانة"),
    ("maint_supervisor", "مشرف الصيانة"),
    ("technician", "فني"),
    ("staff", "موظف قسم / مقدّم بلاغ"),
    ("viewer", "مستخدم عرض وتقارير"),
]

PRIORITIES = [
    ("critical", "حرجة جداً", "#dc2626"),
    ("high", "عالية", "#ea580c"),
    ("medium", "متوسطة", "#d97706"),
    ("low", "منخفضة", "#16a34a"),
]

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Complaint lifecycle (in order)
STATUSES = [
    ("new", "جديد", "#2563eb"),
    ("received", "تم الاستلام", "#0891b2"),
    ("in_progress", "جارٍ العمل عليه", "#7c3aed"),
    ("waiting_parts", "بانتظار قطع الغيار/جهة مختصة", "#d97706"),
    ("completed", "مكتمل", "#16a34a"),
    ("verified", "تم التحقق من الإصلاح", "#059669"),
    ("closed", "مغلق", "#64748b"),
]

STATUS_ORDER = {s[0]: i for i, s in enumerate(STATUSES)}
CANCELLED = "cancelled"
CLOSED_LABEL = "مغلق"

# Fault-type names treated as "other" (the form then asks for a free-text detail)
OTHER_FAULT_NAMES = {"أخرى", "اخرى", "أخري", "غير ذلك", "other"}


def is_other_fault_name(name: str) -> bool:
    return (name or "").strip().lower() in {n.lower() for n in OTHER_FAULT_NAMES}

# Internal notes vs public comments
# Static SLA targets (hours) - editable via settings table at runtime
DEFAULT_SLA_RESPONSE_HOURS = {"critical": 1, "high": 4, "medium": 24, "low": 72}
DEFAULT_SLA_RESOLVE_HOURS = {"critical": 8, "high": 48, "medium": 120, "low": 336}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS hospital_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'staff',
    department    TEXT,
    phone         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

CREATE TABLE IF NOT EXISTS departments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    building   TEXT,
    floor      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS specialties (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS fault_types (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    specialty_id  INTEGER,
    priority      TEXT NOT NULL DEFAULT 'medium',
    sla_hours     INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (specialty_id) REFERENCES specialties(id)
);

CREATE TABLE IF NOT EXISTS technicians (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    specialty_id  INTEGER,
    phone         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (specialty_id) REFERENCES specialties(id)
);

CREATE TABLE IF NOT EXISTS complaints (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no           TEXT NOT NULL UNIQUE,
    reporter_name       TEXT NOT NULL,
    reporter_contact    TEXT NOT NULL,
    reporter_email      TEXT,
    reporter_user_id    INTEGER,
    department          TEXT NOT NULL,
    building            TEXT,
    floor               TEXT,
    room                TEXT,
    location_text       TEXT,
    fault_type_id       INTEGER,
    fault_type          TEXT NOT NULL,
    fault_type_other    TEXT,
    specialty_id        INTEGER,
    specialty           TEXT,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    priority            TEXT NOT NULL DEFAULT 'medium',
    affects_safety      INTEGER NOT NULL DEFAULT 0,
    affects_continuity  INTEGER NOT NULL DEFAULT 0,
    extra_notes         TEXT,
    status              TEXT NOT NULL DEFAULT 'new',
    assigned_technician TEXT,
    assigned_team       TEXT,
    is_demo             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    received_at         TEXT,
    started_at          TEXT,
    resp_target_at      TEXT,
    resolve_target_at   TEXT,
    waiting_at          TEXT,
    completed_at        TEXT,
    verified_at         TEXT,
    closed_at           TEXT,
    cancelled_at        TEXT,
    cancel_reason       TEXT,
    closed_by_user      TEXT,
    FOREIGN KEY (fault_type_id) REFERENCES fault_types(id)
);
CREATE INDEX IF NOT EXISTS idx_complaints_status  ON complaints(status);
CREATE INDEX IF NOT EXISTS idx_complaints_created ON complaints(created_at);
CREATE INDEX IF NOT EXISTS idx_complaints_dept    ON complaints(department);
CREATE INDEX IF NOT EXISTS idx_complaints_prio    ON complaints(priority);

CREATE TABLE IF NOT EXISTS complaint_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    user_name    TEXT,
    action       TEXT NOT NULL,
    from_value   TEXT,
    to_value     TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (complaint_id) REFERENCES complaints(id)
);
CREATE INDEX IF NOT EXISTS idx_events_complaint ON complaint_events(complaint_id);

CREATE TABLE IF NOT EXISTS attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    filename     TEXT NOT NULL,
    stored       TEXT NOT NULL,
    mime         TEXT,
    size_bytes   INTEGER,
    uploaded_at  TEXT NOT NULL,
    FOREIGN KEY (complaint_id) REFERENCES complaints(id)
);
CREATE INDEX IF NOT EXISTS idx_attachments_complaint ON attachments(complaint_id);

CREATE TABLE IF NOT EXISTS internal_notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    user_name    TEXT,
    note         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (complaint_id) REFERENCES complaints(id)
);

CREATE TABLE IF NOT EXISTS notification_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER,
    channel      TEXT NOT NULL,
    recipient    TEXT,
    subject      TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    error        TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token   TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires TEXT NOT NULL,
    created TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""


@contextmanager
def get_db():
    conn = _db.connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_names(table: str) -> list:
    with get_db() as d:
        if _db.is_pg():
            rows = d.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,)
            ).fetchall()
            return [r["column_name"] for r in rows]
        rows = d.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]


def init_db() -> None:
    with get_db() as d:
        script = SCHEMA
        if _db.is_pg():
            script = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        d.executescript(script)
    _migrate()


# Columns added after the first release; applied idempotently on every startup.
_MIGRATIONS = {
    "complaints": [("fault_type_other", "TEXT")],
}


def _migrate() -> None:
    """Add any missing columns to existing tables (SQLite & PostgreSQL)."""
    for table, columns in _MIGRATIONS.items():
        existing = set(_column_names(table))
        for col, coltype in columns:
            if col in existing:
                continue
            with get_db() as d:
                d.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def get_setting(key: str, default: str = "") -> str:
    with get_db() as d:
        row = d.execute(
            "SELECT value FROM hospital_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as d:
        d.execute(
            "INSERT INTO hospital_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def sla_response_hours(priority: str) -> int:
    v = get_setting(f"sla_response_{priority}", "")
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_SLA_RESPONSE_HOURS.get(priority, 24)


def sla_resolve_hours(priority: str) -> int:
    v = get_setting(f"sla_resolve_{priority}", "")
    try:
        return int(v)
    except (TypeError, ValueError):
        return DEFAULT_SLA_RESOLVE_HOURS.get(priority, 120)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def next_number(d, key: str) -> int:
    d.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) "
        "ON CONFLICT(name) DO NOTHING",
        (key,),
    )
    d.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (key,))
    return d.execute("SELECT value FROM counters WHERE name = ?", (key,)).fetchone()["value"]


def next_ticket_no(d) -> str:
    year = datetime.now().year
    n = next_number(d, f"cmp_{year}")
    return f"HM-{year}-{n:05d}"


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    import hashlib

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    import hashlib

    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
        ).hex()
        return hmac_compare(check, digest)
    except Exception:
        return False


def hmac_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)


def create_user(username, password, full_name, role, department=None, phone=None) -> int:
    with get_db() as d:
        cur = d.execute(
            "INSERT INTO users(username, password_hash, full_name, role, department, phone, active, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
            (username, hash_password(password), full_name, role, department, phone, now_iso()),
        )
        return cur.lastrowid


def get_user(user_id: int):
    with get_db() as d:
        return d.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(username: str):
    with get_db() as d:
        return d.execute("SELECT * FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()


def list_users():
    with get_db() as d:
        return d.execute("SELECT * FROM users ORDER BY role, full_name").fetchall()


def update_user(user_id: int, fields: dict) -> bool:
    sets, vals = [], []
    for key in ("full_name", "role", "department", "phone", "active"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            vals.append(str(fields[key]).strip() if key != "active" else int(fields[key]))
    if not sets:
        return False
    vals.append(user_id)
    with get_db() as d:
        cur = d.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
        return cur.rowcount > 0


def set_user_password(user_id: int, password: str) -> None:
    with get_db() as d:
        d.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                  (hash_password(password), user_id))


def create_session(user_id: int, ttl_hours: int = 24 * 7) -> str:
    from datetime import timedelta

    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    with get_db() as d:
        d.execute(
            "INSERT INTO auth_sessions(token, user_id, expires, created) VALUES(?, ?, ?, ?)",
            (token, user_id, expires, now_iso()),
        )
    return token


def get_session(token: str):
    if not token:
        return None
    with get_db() as d:
        row = d.execute(
            "SELECT s.*, u.full_name, u.role, u.username, u.department, u.active "
            "FROM auth_sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?", (token,)
        ).fetchone()
    if row is None or row["expires"] <= now_iso() or not row["active"]:
        return None
    return row


def delete_session(token: str) -> None:
    with get_db() as d:
        d.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Departments / locations / specialties / fault types / technicians
# ---------------------------------------------------------------------------

def list_departments(only_active=False):
    q = "SELECT * FROM departments"
    if only_active:
        q += " WHERE active = 1"
    q += " ORDER BY sort_order, name"
    with get_db() as d:
        return d.execute(q).fetchall()


def add_department(name, building="", floor=""):
    with get_db() as d:
        cur = d.execute(
            "INSERT INTO departments(name, building, floor, active, sort_order) VALUES(?, ?, ?, 1, 0)",
            (name.strip(), building.strip(), floor.strip()),
        )
        return cur.lastrowid


def update_department(dept_id, fields):
    sets, vals = [], []
    for key in ("name", "building", "floor", "sort_order", "active"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            v = fields[key]
            vals.append(int(v) if key in ("sort_order", "active") else str(v).strip())
    if not sets:
        return
    vals.append(dept_id)
    with get_db() as d:
        d.execute(f"UPDATE departments SET {', '.join(sets)} WHERE id = ?", vals)


def delete_department(dept_id):
    with get_db() as d:
        d.execute("DELETE FROM departments WHERE id = ?", (dept_id,))


def list_specialties(only_active=False):
    q = "SELECT * FROM specialties"
    if only_active:
        q += " WHERE active = 1"
    q += " ORDER BY name"
    with get_db() as d:
        return d.execute(q).fetchall()


def add_specialty(name):
    with get_db() as d:
        cur = d.execute("INSERT INTO specialties(name, active) VALUES(?, 1)", (name.strip(),))
        return cur.lastrowid


def update_specialty(spec_id, name=None, active=None):
    sets, vals = [], []
    if name is not None:
        sets.append("name = ?"); vals.append(str(name).strip())
    if active is not None:
        sets.append("active = ?"); vals.append(int(active))
    if not sets:
        return
    vals.append(spec_id)
    with get_db() as d:
        d.execute(f"UPDATE specialties SET {', '.join(sets)} WHERE id = ?", vals)


def delete_specialty(spec_id):
    with get_db() as d:
        d.execute("DELETE FROM specialties WHERE id = ?", (spec_id,))


def list_fault_types(only_active=False):
    q = ("SELECT ft.*, s.name AS specialty_name FROM fault_types ft "
         "LEFT JOIN specialties s ON s.id = ft.specialty_id")
    if only_active:
        q += " WHERE ft.active = 1"
    q += " ORDER BY ft.sort_order, ft.name"
    with get_db() as d:
        rows = d.execute(q).fetchall()
    rows.sort(key=lambda r: (is_other_fault_name(r["name"]),
                             r["sort_order"] or 0, r["name"] or ""))
    return rows


def add_fault_type(name, specialty_id=None, priority="medium", sla_hours=None):
    with get_db() as d:
        cur = d.execute(
            "INSERT INTO fault_types(name, specialty_id, priority, sla_hours, active, sort_order) "
            "VALUES(?, ?, ?, ?, 1, 0)",
            (name.strip(), specialty_id or None, priority,
             (int(sla_hours) if sla_hours else None)),
        )
        return cur.lastrowid


def update_fault_type(ft_id, fields):
    sets, vals = [], []
    for key in ("name", "specialty_id", "priority", "sla_hours", "active", "sort_order"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            v = fields[key]
            if key == "sla_hours":
                vals.append(int(v) if str(v).strip() else None)
            elif key in ("active", "sort_order"):
                vals.append(int(v))
            elif key == "specialty_id":
                vals.append(int(v) if str(v).strip() else None)
            else:
                vals.append(str(v).strip())
    if not sets:
        return
    vals.append(ft_id)
    with get_db() as d:
        d.execute(f"UPDATE fault_types SET {', '.join(sets)} WHERE id = ?", vals)


def delete_fault_type(ft_id):
    with get_db() as d:
        d.execute("DELETE FROM fault_types WHERE id = ?", (ft_id,))


def list_technicians(only_active=False, specialty_id=None):
    q = ("SELECT t.*, s.name AS specialty_name FROM technicians t "
         "LEFT JOIN specialties s ON s.id = t.specialty_id")
    conds, params = [], []
    if only_active:
        conds.append("t.active = 1")
    if specialty_id:
        conds.append("t.specialty_id = ?")
        params.append(int(specialty_id))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY t.active DESC, t.name"
    with get_db() as d:
        return d.execute(q, params).fetchall()


def add_technician(name, specialty_id=None, phone=None):
    with get_db() as d:
        cur = d.execute(
            "INSERT INTO technicians(name, specialty_id, phone, active) VALUES(?, ?, ?, 1)",
            (name.strip(), specialty_id or None, phone.strip() or None),
        )
        return cur.lastrowid


def update_technician(tech_id, fields):
    sets, vals = [], []
    for key in ("name", "specialty_id", "phone", "active"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            v = fields[key]
            if key == "specialty_id":
                vals.append(int(v) if str(v).strip() else None)
            elif key == "active":
                vals.append(int(v))
            else:
                vals.append(str(v).strip())
    if not sets:
        return
    vals.append(tech_id)
    with get_db() as d:
        d.execute(f"UPDATE technicians SET {', '.join(sets)} WHERE id = ?", vals)


def delete_technician(tech_id):
    with get_db() as d:
        d.execute("DELETE FROM technicians WHERE id = ?", (tech_id,))


# ---------------------------------------------------------------------------
# Complaints
# ---------------------------------------------------------------------------

FIRST_UPLOAD = False


def ensure_first_admin() -> None:
    """Create a default admin if no user exists yet (first-run bootstrap)."""
    with get_db() as d:
        n = d.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if n == 0:
        create_user(
            "admin", "admin123", "مدير النظام",
            "admin", "الإدارة",
        )
        set_setting("boot_admin_notice", "أُنشئ حساب المدير الافتراضي (admin / admin123) — غيّره فوراً")


def require_login(token):
    return get_session(token)


def role_can(role: str, required: str) -> bool:
    """Very simple hierarchy: admin > maint_manager > maint_supervisor > staff/viewer."""
    rank = {
        "viewer": 0, "staff": 1,
        "technician": 2, "maint_supervisor": 3,
        "maint_manager": 4, "admin": 5,
    }
    return rank.get(role, -1) >= rank.get(required, 9)


def create_complaint(data: dict, d=None) -> int:
    internal = d is not None
    d = d or (lambda: __import__("contextlib").nullcontext())
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO complaints (
                ticket_no, reporter_name, reporter_contact, reporter_email, reporter_user_id,
                department, building, floor, room, location_text,
                fault_type_id, fault_type, fault_type_other, specialty_id, specialty,
                title, description, priority, affects_safety, affects_continuity, extra_notes,
                status, assigned_technician, assigned_team, is_demo,
                created_at, resp_target_at, resolve_target_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_ticket_no(conn),
                data["reporter_name"].strip(),
                data["reporter_contact"].strip(),
                (data.get("reporter_email") or "").strip() or None,
                data.get("reporter_user_id"),
                data["department"].strip(),
                (data.get("building") or "").strip() or None,
                (data.get("floor") or "").strip() or None,
                (data.get("room") or "").strip() or None,
                (data.get("location_text") or "").strip() or None,
                data.get("fault_type_id"),
                data["fault_type"].strip(),
                (data.get("fault_type_other") or "").strip() or None,
                data.get("specialty_id"),
                (data.get("specialty") or "").strip() or None,
                data.get("title", "").strip() or "",
                data["description"].strip(),
                data["priority"],
                1 if data.get("affects_safety") else 0,
                1 if data.get("affects_continuity") else 0,
                (data.get("extra_notes") or "").strip() or None,
                "new",
                (data.get("assigned_technician") or "").strip() or None,
                (data.get("assigned_team") or "").strip() or None,
                1 if data.get("is_demo") else 0,
                now_iso(),
                data.get("resp_target_at"),
                data.get("resolve_target_at"),
            ),
        )
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO complaint_events(complaint_id, user_name, action, to_value, created_at) "
            "VALUES(?, ?, 'created', 'new', ?)",
            (cid, data.get("reporter_name", "مقدم البلاغ"), now_iso()),
        )
        return cid


def _complaint_base_query(with_where=True):
    q = "SELECT c.* FROM complaints c"


def count_complaints_total() -> int:
    with get_db() as d:
        return d.execute("SELECT COUNT(*) AS c FROM complaints").fetchone()["c"]


def reset_complaints() -> dict:
    """Delete every complaint and its children, reset the ticket counters so the
    next complaint starts again at HM-<year>-00001.

    Returns a summary dict with the number of removed rows and the list of
    stored attachment paths (so the caller can unlink the files on disk).
    """
    with get_db() as d:
        stored = [r["stored"] for r in d.execute("SELECT stored FROM attachments").fetchall()]
        counts = {}
        for table in ("attachments", "internal_notes", "complaint_events",
                      "notification_log", "complaints"):
            counts[table] = d.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            d.execute(f"DELETE FROM {table}")
        d.execute("DELETE FROM counters")
    return {"counts": counts, "stored": stored}


def build_filters(args: dict) -> tuple:
    """Return (WHERE clause, params) from filter dict for complaint listing."""
    conds, params = [], []
    if args.get("status") and args["status"] != "all":
        if args["status"] == CANCELLED:
            conds.append("c.status = 'cancelled'")
        else:
            conds.append("c.status = ?")
            params.append(args["status"])
    if args.get("priority") and args["priority"] != "all":
        conds.append("c.priority = ?")
        params.append(args["priority"])
    if args.get("department") and args["department"] != "all":
        conds.append("c.department = ?")
        params.append(args["department"])
    if args.get("specialty") and args["specialty"] != "all":
        conds.append("c.specialty = ?")
        params.append(args["specialty"])
    if args.get("q") and args["q"].strip():
        like = f"%{args['q'].strip()}%"
        conds.append("(c.ticket_no LIKE ? OR c.title LIKE ? OR c.description LIKE ? OR "
                     "c.reporter_name LIKE ? OR c.department LIKE ?)")
        params += [like, like, like, like, like]
    date_from = args.get("date_from")
    if date_from:
        conds.append("c.created_at >= ?")
        params.append(f"{date_from} 00:00:00")
    date_to = args.get("date_to")
    if date_to:
        conds.append("c.created_at <= ?")
        params.append(f"{date_to} 23:59:59")
    if args.get("demo") == "real":
        conds.append("c.is_demo = 0")
    elif args.get("demo") == "demo":
        conds.append("c.is_demo = 1")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def list_complaints(filters=None, limit: int = 200, offset: int = 0, order: str = "recency"):
    filters = filters or {}
    where, params = build_filters(filters)
    if order == "priority_first":
        order_sql = "CASE c.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, c.created_at DESC"
    else:
        order_sql = "c.created_at DESC"
    q = (f"SELECT c.* FROM complaints c {where} ORDER BY {order_sql}, c.id DESC "
         f"LIMIT {int(limit)} OFFSET {int(offset)}")
    with get_db() as d:
        return d.execute(q, params).fetchall()


def count_complaints(filters=None) -> int:
    filters = filters or {}
    where, params = build_filters(filters)
    q = f"SELECT COUNT(*) AS c FROM complaints c {where}"
    with get_db() as d:
        return d.execute(q, params).fetchone()["c"]


def get_complaint(cid: int):
    with get_db() as d:
        return d.execute("SELECT c.* FROM complaints c WHERE c.id = ?", (cid,)).fetchone()


def get_complaint_by_ticket(ticket: str):
    with get_db() as d:
        return d.execute("SELECT c.* FROM complaints c WHERE c.ticket_no = ?", (ticket,)).fetchone()


def update_complaint(cid: int, fields: dict, actor=None) -> list:
    """Apply state changes, record events, return list of events to broadcast."""
    events = []
    with get_db() as d:
        row = d.execute("SELECT * FROM complaints WHERE id = ?", (cid,)).fetchone()
        if row is None:
            return events

        sets, vals = [], []
        status = fields.get("status", row["status"])
        old_status = row["status"]
        t = now_iso()

        # Status-driven timestamps
        if status != old_status and status in ("received", "in_progress", "waiting_parts",
                                                "completed", "verified", "closed", CANCELLED):
            if status in ("received", "in_progress", "waiting_parts", "completed",
                          "verified", "closed", CANCELLED) and not row["received_at"]:
                sets.append("received_at = ?"); vals.append(t)
            if status in ("in_progress", "waiting_parts", "completed", "verified", "closed") and not row["started_at"]:
                sets.append("started_at = ?"); vals.append(t)
            if status == "waiting_parts":
                sets.append("waiting_at = ?"); vals.append(t)
            if status == "completed":
                sets.append("completed_at = ?"); vals.append(t)
                sets.append("verified_at = ?"); vals.append(t)
            if status == "closed":
                sets.append("closed_at = ?"); vals.append(t)
                sets.append("closed_by_user = ?"); vals.append(actor or "")
            if status == CANCELLED:
                sets.append("cancelled_at = ?"); vals.append(t)

        mapping = {
            "assigned_technician": fields.get("assigned_technician", row["assigned_technician"]),
            "assigned_team": fields.get("assigned_team", row["assigned_team"]),
            "specialty": fields.get("specialty", row["specialty"]),
            "priority": fields.get("priority", row["priority"]),
            "title": fields.get("title", row["title"]),
            "description": fields.get("description", row["description"]),
            "extra_notes": fields.get("extra_notes", row["extra_notes"]),
            "cancel_reason": fields.get("cancel_reason", row["cancel_reason"]),
            "department": fields.get("department", row["department"]),
            "fault_type": fields.get("fault_type", row["fault_type"]),
            "fault_type_other": fields.get("fault_type_other", row["fault_type_other"]),
        }
        for key in ("assigned_technician", "assigned_team", "specialty", "priority",
                    "title", "description", "extra_notes", "cancel_reason", "department",
                    "fault_type", "fault_type_other"):
            val = mapping.get(key)
            if val is not None and str(val).strip() != str(row[key] or ""):
                sets.append(f"{key} = ?")
                vals.append(str(val).strip() or None)
                events.append((key, row[key], str(val).strip() or None))

        status_changed = status != old_status
        if status_changed:
            sets.append("status = ?"); vals.append(status)
            events.append(("status", old_status, status))

        if not sets:
            return events
        vals.append(cid)
        d.execute(f"UPDATE complaints SET {', '.join(sets)} WHERE id = ?", vals)

        # Audit event
        if status_changed:
            d.execute(
                "INSERT INTO complaint_events(complaint_id, user_name, action, from_value, to_value, created_at) "
                "VALUES(?, ?, 'status_change', ?, ?, ?)",
                (cid, actor or "النظام", old_status, status, t),
            )
        for key, from_v, to_v in events:
            d.execute(
                "INSERT INTO complaint_events(complaint_id, user_name, action, from_value, to_value, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (cid, actor or "النظام", {"status": "status_change"}.get(key, f"update_{key}"),
                 from_v, to_v, t),
            )
    return events


def add_internal_note(cid: int, user: str, note: str) -> None:
    with get_db() as d:
        d.execute(
            "INSERT INTO internal_notes(complaint_id, user_name, note, created_at) VALUES(?, ?, ?, ?)",
            (cid, user, note, now_iso()),
        )
        d.execute(
            "INSERT INTO complaint_events(complaint_id, user_name, action, notes, created_at) "
            "VALUES(?, ?, 'internal_note', ?, ?)",
            (cid, user, note[:2000], now_iso()),
        )


def list_complaint_events(cid: int):
    with get_db() as d:
        return d.execute(
            "SELECT * FROM complaint_events WHERE complaint_id = ? ORDER BY id ASC", (cid,)
        ).fetchall()


def list_internal_notes(cid: int):
    with get_db() as d:
        return d.execute(
            "SELECT * FROM internal_notes WHERE complaint_id = ? ORDER BY id DESC", (cid,)
        ).fetchall()


def add_attachment(cid: int, filename: str, stored: str, mime: str, size: int) -> None:
    with get_db() as d:
        d.execute(
            "INSERT INTO attachments(complaint_id, filename, stored, mime, size_bytes, uploaded_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (cid, filename, stored, mime, size, now_iso()),
        )
        d.execute(
            "INSERT INTO complaint_events(complaint_id, user_name, action, notes, created_at) "
            "VALUES(?, ?, 'attachment', ?, ?)",
            (cid, "النظام", f"مرفق: {filename}", now_iso()),
        )


def list_attachments(cid: int):
    with get_db() as d:
        return d.execute(
            "SELECT * FROM attachments WHERE complaint_id = ? ORDER BY id ASC", (cid,)
        ).fetchall()


def get_attachment(aid: int):
    with get_db() as d:
        return d.execute("SELECT * FROM attachments WHERE id = ?", (aid,)).fetchone()


def delete_attachment(aid: int):
    with get_db() as d:
        d.execute("DELETE FROM attachments WHERE id = ?", (aid,))


# ---------------------------------------------------------------------------
# Notifications log
# ---------------------------------------------------------------------------

def log_notification(complaint_id, channel, recipient, subject, status, error=""):
    with get_db() as d:
        d.execute(
            "INSERT INTO notification_log(complaint_id, channel, recipient, subject, status, error, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (complaint_id, channel, recipient, subject, status, (error or "")[:2000], now_iso()),
        )


def list_notifications(limit=100):
    with get_db() as d:
        return d.execute(
            "SELECT n.*, c.ticket_no FROM notification_log n "
            "LEFT JOIN complaints c ON c.id = n.complaint_id "
            "ORDER BY n.id DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Stats / analytics
# ---------------------------------------------------------------------------

def stats(filters=None) -> dict:
    filters = filters or {}
    base_where, base_params = build_filters(filters)
    with get_db() as d:
        total = d.execute(f"SELECT COUNT(*) AS c FROM complaints c {base_where}", base_params).fetchone()["c"]
        by_status = {}
        for s, _, _ in STATUSES + [(CANCELLED, "", "")]:
            w = base_where + (" AND " if base_where else " WHERE ") + "c.status = ?"
            by_status[s] = d.execute(f"SELECT COUNT(*) AS c FROM complaints c {w}", base_params + [s]).fetchone()["c"]
        by_priority = {}
        for p, _, _ in PRIORITIES:
            w = base_where + (" AND " if base_where else " WHERE ") + "c.priority = ?"
            by_priority[p] = d.execute(f"SELECT COUNT(*) AS c FROM complaints c {w}", base_params + [p]).fetchone()["c"]

        # Public-facing vs demo
        demo = d.execute(
            f"SELECT COUNT(*) AS c FROM complaints c {base_where + (' AND ' if base_where else ' WHERE ') + 'c.is_demo = 1'}",
            base_params
        ).fetchone()["c"]

    open_set = {s for s, _, _ in STATUSES if s not in ("closed",)}
    open_count = sum(by_status.get(s, 0) for s in open_set)
    closed = by_status.get("closed", 0)
    completed_all = by_status.get("completed", 0) + closed
    pct = round(completed_all * 100 / total) if total else 0
    return {
        "total": total,
        "open": open_count,
        "closed": closed,
        "completed": completed_all,
        "pct": pct,
        "by_status": by_status,
        "by_priority": by_priority,
        "demo": demo,
    }


def status_counts() -> dict:
    return stats()["by_status"]


def ticker_summary():
    """Lightweight counts for the header/nav badges."""
    st = status_counts()
    return {"new": st.get("new", 0), "critical": stats()["by_priority"].get("critical", 0)}


def recent_complaints(limit=12):
    with get_db() as d:
        return d.execute(
            "SELECT c.* FROM complaints c WHERE c.is_demo = 0 ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def overdue_complaints(limit=100):
    """Complaints not yet closed/completed whose response target has passed."""
    t = now_iso()
    with get_db() as d:
        return d.execute(
            """SELECT c.* FROM complaints c
               WHERE c.is_demo = 0 AND c.status NOT IN ('closed', 'completed', 'verified', 'cancelled')
                 AND c.resp_target_at IS NOT NULL AND c.resp_target_at < ?
               ORDER BY c.resp_target_at ASC LIMIT ?""",
            (t, limit),
        ).fetchall()


def complaints_by_department(limit=10):
    with get_db() as d:
        return d.execute(
            """SELECT department AS name, COUNT(*) AS count FROM complaints
               GROUP BY department ORDER BY count DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def complaints_by_fault_type(limit=10):
    with get_db() as d:
        return d.execute(
            """SELECT fault_type AS name, COUNT(*) AS count FROM complaints
               GROUP BY fault_type ORDER BY count DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def complaints_by_priority():
    with get_db() as d:
        return d.execute(
            """SELECT priority AS name, COUNT(*) AS count FROM complaints
               GROUP BY priority ORDER BY count DESC""",
        ).fetchall()


def technicians_performance():
    with get_db() as d:
        rows = d.execute(
            """SELECT assigned_technician AS name, COUNT(*) AS total,
                      SUM(CASE WHEN status IN ('verified','closed') THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN status NOT IN ('closed','verified','cancelled') THEN 1 ELSE 0 END) AS open
                 FROM complaints
                 WHERE assigned_technician IS NOT NULL AND assigned_technician <> ''
                 GROUP BY assigned_technician
                 ORDER BY total DESC"""
        ).fetchall()
    res = []
    for r in rows:
        done = r["done"] or 0
        res.append({
            "name": r["name"],
            "total": r["total"],
            "done": done,
            "open": r["open"] or 0,
            "pct": round(done * 100 / r["total"]) if r["total"] else 0,
        })
    return res


def _epoch_diff_hours(field_a: str, field_b: str) -> str:
    """Return SQL expression for (a - b) in hours across SQLite and PostgreSQL."""
    if _db.is_pg():
        return (f"(EXTRACT(EPOCH FROM (({field_a})::timestamptz - "
                f"({field_b})::timestamptz)) / 3600.0)")
    return f"((strftime('%s', {field_a}) - strftime('%s', {field_b})) / 3600.0)"


def avg_response_hours() -> float:
    """Average time from created_at -> received_at (hours) for received complaints."""
    with get_db() as d:
        expr = _epoch_diff_hours("received_at", "created_at")
        row = d.execute(f"SELECT AVG({expr}) AS avg FROM complaints WHERE received_at IS NOT NULL").fetchone()
    if row is None or row["avg"] is None:
        return 0.0
    return round(row["avg"], 2)


def avg_resolve_hours() -> float:
    with get_db() as d:
        expr = _epoch_diff_hours("completed_at", "created_at")
        row = d.execute(f"SELECT AVG({expr}) AS avg FROM complaints WHERE completed_at IS NOT NULL").fetchone()
    if row is None or row["avg"] is None:
        return 0.0
    return round(row["avg"], 2)


def recurring_locations(limit=8):
    with get_db() as d:
        return d.execute(
            """SELECT location_text AS location, COUNT(*) AS count FROM complaints
               WHERE location_text IS NOT NULL AND location_text <> ''
               GROUP BY location_text ORDER BY count DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def complaints_over_time(days: int = 30) -> list:
    from datetime import timedelta

    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_db() as d:
        rows = d.execute(
            """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
               FROM complaints WHERE created_at >= ? GROUP BY day ORDER BY day""",
            (start,),
        ).fetchall()
    return [{"day": r["day"], "count": r["count"]} for r in rows]


def report_rows(filters=None) -> list:
    """All matching complaints sorted newest first — used by Excel/PDF export."""
    filters = filters or {}
    where, params = build_filters(filters)
    q = f"SELECT c.* FROM complaints c {where} ORDER BY c.created_at DESC"
    with get_db() as d:
        return d.execute(q, params).fetchall()