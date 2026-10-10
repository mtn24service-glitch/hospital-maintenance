"""Unified database connection layer for the Hospital Maintenance system.

Works over SQLite locally (default) and PostgreSQL in the cloud when the
``DATABASE_URL`` environment variable is present (Neon / Render / Replit).

The connection surface mirrors what app/database.py expects (``.execute``,
``.executescript``, ``.commit``, ``.close``, ``row_factory``, ``cursor.rowcount``
and ``cursor.lastrowid``) so the rest of the app stays engine-agnostic.
"""

import os
import threading
import time

_shared = None
_shared_lock = threading.RLock()

# Tables that have an `id` column and rely on cursor.lastrowid (via RETURNING id).
_TABLES_WITH_ID = {
    "users", "departments", "specialties", "fault_types", "technicians",
    "complaints", "complaint_events", "attachments", "internal_notes",
    "notification_log",
}


def _insert_table(sql: str):
    import re

    m = re.match(r"^\s*insert\s+into\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _load_env() -> None:
    """Load .env lazily in local dev so DATABASE_URL etc. are picked up."""
    if os.environ.get("DATABASE_URL"):
        return
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        if key.strip() and not os.environ.get(key.strip()):
            os.environ[key.strip()] = val


def is_pg() -> bool:
    _load_env()
    return bool(os.environ.get("DATABASE_URL"))


class _PgRow(dict):
    def keys(self):  # noqa: A003
        return dict.__iter__(self)


class _PgConnection:
    """Adapter making a psycopg connection look like sqlite3 for the subset
    of calls used by app/database.py.

    The underlying PostgreSQL connection is *shared* and long-lived: it is
    never torn down by connect()/close() (get_db() calls conn.close() after
    every block), and all calls are serialized with a lock so concurrent
    request threads cannot interleave on a single psycopg connection.
    """

    def __init__(self, raw):
        self._raw = raw
        self._tmp = None
        self._lock = threading.RLock()
        self.closed = False

    def execute(self, sql, params=()):
        global _shared
        conn = self
        with self._lock:
            for attempt in range(1, 5):
                try:
                    return conn._execute(sql, params)
                except Exception as exc:
                    if not _is_conn_error(exc) or attempt == 4:
                        raise
                    time.sleep(attempt)
                    try:
                        with _shared_lock:
                            _shared = _create_pg()
                            conn = _shared
                    except Exception:
                        time.sleep(2)

    def _execute(self, sql, params=()):
        sql, params = _translate(sql, params)
        cur = self._raw.cursor()
        try:
            self._tmp = cur
            lower = sql.lstrip().lower()
            if lower.startswith("insert") and "returning " not in lower \
                    and _insert_table(sql) in _TABLES_WITH_ID:
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
            if isinstance(params, (tuple, list)) and len(params) == 0:
                cur.execute(sql)
            else:
                cur.execute(sql, list(params) if isinstance(params, (tuple, list)) else params)
            rows = []
            rowcount = getattr(cur, "rowcount", 0)
            lastrowid = None
            if cur.description is not None:
                rows = cur.fetchall()
                ci = cur.description
                if lower.startswith(("insert", "update", "delete", "select")):
                    if ci and len(ci) == 1 and ci[0].name.lower() in ("id",):
                        try:
                            v = rows[0].get("id") if rows else None
                            if v is not None:
                                lastrowid = v
                        except Exception:
                            pass
            return _PgResult(self, rows, rowcount, lastrowid)
        except Exception:
            cur.close()
            raise

    def executescript(self, sql):
        for stmt in _split_statements(sql):
            if stmt.strip():
                self.execute(stmt)
        return self

    def commit(self):
        with self._lock:
            try:
                self._raw.commit()
            except Exception as exc:
                # autocommit=True -> a lost/idle connection has nothing to
                # flush; ignore it here and let the next execute() reconnect.
                if not _is_conn_error(exc):
                    raise
        return self

    def close(self):
        """No-op for the shared PostgreSQL connection: get_db() calls close()
        after every block but the singleton must stay alive for the process."""
        self.closed = False
        return self

    def cursor(self):
        return self._raw.cursor()


class _PgResult:
    def __init__(self, conn, rows, rowcount=0, lastrowid=None):
        self._conn = conn
        self._rows = [_PgRow(dict(r)) for r in rows]
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    @property
    def returned_last_rowid(self):
        return self.lastrowid


def _translate(sql, params):
    if "ON CONFLICT(" in sql:
        sql = sql.replace("ON CONFLICT(name) DO NOTHING", "ON CONFLICT (name) DO NOTHING")
        sql = sql.replace("ON CONFLICT(name) DO UPDATE", "ON CONFLICT (name) DO UPDATE")
    if "?" in sql:
        sql = sql.replace("?", "%s")
    return sql, params


def _split_statements(sql):
    parts, buf = [], []
    for ch in sql:
        buf.append(ch)
        if ch == ";":
            parts.append("".join(buf))
            buf = []
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def connect():
    """Return an open connection (SQLite by default, PostgreSQL if set).

    For PostgreSQL a single shared connection is reused to avoid the heavy
    per-request setup on serverless Neon compute.
    """
    if is_pg():
        broken = (
            _shared is None
            or getattr(_shared, "_broken", False)
            or getattr(_shared, "closed", False)
            or bool(getattr(getattr(_shared, "_raw", None), "closed", False))
        )
        if broken:
            _reconnect_pg()
        return _shared

    import sqlite3
    from pathlib import Path

    db_path = Path(__file__).resolve().parent.parent / "hospital_maintenance.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_pg():
    """Open a brand-new PostgreSQL connection and wrap it in the adapter."""
    import psycopg
    import psycopg.rows

    url = os.environ["DATABASE_URL"]
    raw = psycopg.connect(url, autocommit=True, row_factory=psycopg.rows.dict_row)
    return _PgConnection(raw)


def _reconnect_pg():
    """Replace the shared PG connection with a fresh one. Returns the new
    connection, or None if the shared one is still healthy."""
    global _shared
    with _shared_lock:
        conn = _create_pg()
        _shared = conn
        return conn


def _is_conn_error(exc) -> bool:
    """Connection-level failures (terminated session, closed connection, dropped
    network) that warrant creating a fresh PostgreSQL connection."""
    try:
        import psycopg

        if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
            return True
    except Exception:
        pass
    name = type(exc).__name__
    low = str(exc).lower()
    return name in ("OperationalError", "InterfaceError", "AdminShutdown") \
        or "connection is closed" in low or "terminat" in low