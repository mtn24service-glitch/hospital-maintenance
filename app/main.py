"""Hospital Maintenance & Operations complaint system - FastAPI application.

Prince Mohammed Bin Nasser Hospital - Jazan.

Run locally:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Cloud (Neon DB): set DATABASE_URL in .env (done) and deploy to Render/Replit.
"""

import asyncio
import os
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database as db
from . import exports as ex
from . import i18n
from . import notifier, realtime

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LIVE_EXCEL_PATH = DATA_DIR / "complaints_live.xlsx"

PORT = int(os.environ.get("PORT", "8000"))
SESSION_COOKIE = "hm_session"
LANG_COOKIE = "hm_lang"

db.init_db()
db.ensure_first_admin()

app = FastAPI(title="نظام إدارة بلاغات الصيانة والتشغيل - مستشفى الأمير محمد بن ناصر",
              docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ALLOWED_IMG = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ALLOWED_VIDEO = {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"}
MAX_FILE_BYTES = 15 * 1024 * 1024  # 15 MB
PRIORITY_KEYS = {p for p, _, _ in db.PRIORITIES}


class _Redirect(Exception):
    """Raised to perform an HTTP redirect (Starlette no longer supports
    ``raise RedirectResponse`` from sync endpoints)."""

    def __init__(self, url: str, status_code: int = 303):
        self.url = url
        self.status_code = status_code
        super().__init__(url)


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.url, status_code=exc.status_code)


class _Forbidden(Exception):
    """Raised when an authenticated (or anonymous) user lacks admin rights."""

    def __init__(self, message: str = ""):
        self.message = message
        super().__init__(message)


@app.exception_handler(_Forbidden)
async def _forbidden_handler(request: Request, exc: _Forbidden):
    return render(request, "error.html", {
        "code": 403,
        "message": exc.message or "هذه الصفحة متاحة لمدير النظام فقط.",
    }, status_code=403)


def current_lang(request: Request) -> str:
    lang = request.cookies.get(LANG_COOKIE, i18n.DEFAULT_LANG)
    return lang if lang in i18n.LANGS else i18n.DEFAULT_LANG


def public_base(request: Request) -> str:
    """Absolute base URL used inside QR codes / printed links."""
    base = (os.environ.get("BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    return str(request.base_url).rstrip("/")


def track_url(request: Request, ticket_no: str) -> str:
    return f"{public_base(request)}/track/{ticket_no}"


def save_live_excel() -> None:
    """Rewrite the auto-exported Excel snapshot of every complaint."""
    try:
        data = ex.build_live_excel({"demo": "all"})
        LIVE_EXCEL_PATH.write_bytes(data)
    except Exception:
        pass


def status_ar(status: str) -> str:
    for s, ar, _ in db.STATUSES + [(db.CANCELLED, "ملغي", "")]:
        if s == status:
            return ar
    return status


def priority_ar(priority: str) -> str:
    for p, ar, _ in db.PRIORITIES:
        if p == priority:
            return ar
    return priority


templates.env.globals.update(
    statuses=db.STATUSES,
    priorities=db.PRIORITIES,
    roles=db.ROLES,
    status_ar=status_ar,
    priority_ar=priority_ar,
    cancelled=db.CANCELLED,
    fmt_dt=ex._fmt,
    now=datetime.now,
    prio_ar={p: ar for p, ar, _ in db.PRIORITIES},
    stat_ar={s: ar for s, ar, _ in db.STATUSES},
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    return db.get_session(token)


def require_role(user, *roles):
    if user is None or user["role"] not in roles:
        return False
    return True


def login_required(request: Request):
    user = current_user(request)
    if user is None:
        raise _Redirect("/login?next=" + urllib.parse.quote(request.url.path))
    return user


def admin_required(request: Request):
    """Only the system administrator may open the dashboard and admin areas."""
    user = current_user(request)
    if user is None:
        raise _Redirect("/login?next=" + urllib.parse.quote(request.url.path))
    if user["role"] != "admin":
        raise _Forbidden("لوحة التحكم مقصورة على مدير النظام. لا تملك صلاحية الوصول.")
    return user


def role_hierarchy_ok(user, minimum: str) -> bool:
    rank = {"viewer": 0, "staff": 1, "technician": 2,
            "maint_supervisor": 3, "maint_manager": 4, "admin": 5}
    return rank.get(user["role"], -1) >= rank.get(minimum, 99)


def user_can_manage(user) -> bool:
    return role_hierarchy_ok(user, "maint_supervisor")


def user_can_admin(user) -> bool:
    return user["role"] in ("admin", "maint_manager")


def render(request, name, ctx=None, status_code=200):
    user = current_user(request)
    lang = current_lang(request)
    t = i18n.translator(lang)
    ctx = dict(ctx or {})
    ctx["user"] = user
    ctx["lang"] = lang
    ctx["dir"] = "rtl" if lang == "ar" else "ltr"
    ctx["t"] = t
    ctx["is_admin"] = bool(user and user["role"] == "admin")
    ctx["notif_channels"] = notifier.config_status(lang)
    ctx["js_strings"] = i18n.js_strings(lang)
    # Language-aware status / priority / role labels (override the AR globals).
    statuses = [(k, t(f"status.{k}"), color) for k, _ar, color in db.STATUSES]
    priorities = [(k, t(f"prio.{k}"), color) for k, _ar, color in db.PRIORITIES]
    ctx["statuses"] = statuses
    ctx["priorities"] = priorities
    ctx["roles"] = [(k, t(f"role.{k}")) for k, _ar in db.ROLES]
    stat_map = {k: lbl for k, lbl, _ in statuses}
    prio_map = {k: lbl for k, lbl, _ in priorities}
    ctx["stat_ar"] = stat_map
    ctx["prio_ar"] = prio_map
    ctx["status_ar"] = lambda s: t("status.cancelled") if s == db.CANCELLED else stat_map.get(s, s)
    ctx["priority_ar"] = lambda p: prio_map.get(p, p)
    return templates.TemplateResponse(
        request, name, ctx, status_code=status_code
    )


def redirect(url: str):
    return RedirectResponse(url=url, status_code=303)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query(default="/")):
    if current_user(request):
        return redirect(next if next != "/login" else "/")
    return render(request, "login.html", {"next": next})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/")):
    user = db.get_user_by_username(username.strip())
    if user is None or not db.verify_password(password, user["password_hash"]):
        return render(request, "login.html",
                      {"error": "اسم المستخدم أو كلمة المرور غير صحيحة", "next": next},
                      status_code=401)
    if not user["active"]:
        return render(request, "login.html",
                      {"error": "هذا الحساب معطّل، تواصل مع مدير النظام", "next": next},
                      status_code=403)
    if user["role"] != "admin":
        return render(request, "login.html",
                      {"error": "لوحة التحكم مقصورة على مدير النظام. لا تملك صلاحية الدخول.", "next": next},
                      status_code=403)
    token = db.create_session(user["id"])
    target = next if (next.startswith("/") and next not in ("/", "/login")) else (
        "/dashboard" if user["role"] == "admin" else "/")
    resp = redirect(target)
    resp.set_cookie(SESSION_COOKIE, token, max_age=7 * 24 * 3600, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.delete_session(token)
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Real-time stream (SSE)
# ---------------------------------------------------------------------------

@app.get("/api/stream")
async def stream(request: Request):
    user = current_user(request)
    if user is None or user["role"] != "admin":
        raise HTTPException(status_code=401, detail="unauthorized")
    cid, queue = realtime.add_client()
    request.app.state.queue = queue

    async def gen():
        try:
            # initial connection heartbeat merged with a hello event
            yield "retry: 3000\n\n"
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield payload
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            realtime.remove_client(cid)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ---------------------------------------------------------------------------
# Public complaint form
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "landing.html")


@app.get("/report", response_class=HTMLResponse)
def report_form(request: Request):
    return render(request, "report_form.html", {
        "departments": db.list_departments(only_active=True),
        "fault_types": db.list_fault_types(only_active=True),
        "specialties": db.list_specialties(only_active=True),
    })


async def _save_upload(file: UploadFile, prefix="att") -> str:
    """Save an uploaded file (image/video) into static/uploads, return relative path."""
    if not file or not file.filename:
        return ""
    ctype = file.content_type or ""
    ext_map = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/webm": ".webm",
        "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    }
    if ctype not in ALLOWED_IMG and ctype not in ALLOWED_VIDEO:
        raise ValueError("نوع الملف غير مدعوم — يُسمح بالصور (JPG/PNG/GIF/WebP) والفيديو (MP4/WebM/MOV/MKV)")
    data = await file.read()
    if not data:
        return ""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("حجم الملف يتجاوز 15 ميجابايت")
    name = f"{prefix}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}{ext_map.get(ctype, '.bin')}"
    (UPLOAD_DIR / name).write_bytes(data)
    return f"/static/uploads/{name}"


def _recent_duplicate(reporter_name, description):
    """Return the most recent complaint with same reporter+description in last 120s."""
    from datetime import timedelta

    since = (datetime.now() - timedelta(seconds=120)).isoformat(timespec="seconds")
    with db.get_db() as d:
        row = d.execute(
            """SELECT * FROM complaints
               WHERE reporter_name = ? AND description = ? AND created_at >= ?
               ORDER BY id DESC LIMIT 1""",
            (reporter_name.strip(), description.strip(), since),
        ).fetchone()
    return row


def post_report_hook(complaint, event="new_complaint"):
    """Fire the realtime broadcast without blocking the request thread."""
    import asyncio

    async def _run():
        try:
            if event == "new_complaint":
                await realtime.emit_new_complaint(complaint)
            else:
                await realtime.emit_status_change(complaint["id"], {"status": complaint["status"]})
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_run())
        else:
            loop.run_until_complete(_run())
    except Exception:
        pass


@app.post("/report")
async def report_submit(
    request: Request,
    reporter_name: str = Form(...),
    reporter_contact: str = Form(...),
    reporter_email: str = Form(""),
    department: str = Form(...),
    building: str = Form(""),
    floor: str = Form(""),
    room: str = Form(""),
    fault_type_id: int = Form(...),
    fault_type_other: str = Form(""),
    priority: str = Form("medium"),
    title: str = Form(""),
    description: str = Form(...),
    affects_safety: str = Form(""),
    affects_continuity: str = Form(""),
    extra_notes: str = Form(""),
    is_demo: str = Form(""),
    attachments: list[UploadFile] = File(default=[]),
):
    user = current_user(request)

    # Guard against duplicate slow submits: check last ticket by this reporter+department
    dup = _recent_duplicate(reporter_name.strip(), description.strip())
    if dup:
        return render(request, "report_success.html", {
            "complaint": dup,
            "track_url": track_url(request, dup["ticket_no"]),
            "dup_warning": "تم إرسال هذا البلاغ مسبقاً قبل أقل من دقيقتين — عُرض عليك الرقم الحالي فعلياً.",
        })

    ft = None
    for row in db.list_fault_types():
        if row["id"] == fault_type_id:
            ft = row
            break
    if ft is None:
        raise HTTPException(status_code=400, detail="fault type not found")

    # "Other" fault type -> require and fold in the free-text description.
    fault_other = fault_type_other.strip()
    if db.is_other_fault_name(ft["name"]):
        if not fault_other:
            return render(request, "report_form.html", {
                "error": "عند اختيار «أخرى» يجب كتابة وصف نوع العطل.",
                "departments": db.list_departments(only_active=True),
                "fault_types": db.list_fault_types(only_active=True),
                "specialties": db.list_specialties(only_active=True),
                "form": {"fault_type_id": fault_type_id, "fault_type_other": fault_other},
            }, status_code=400)
        fault_name = f"{ft['name']}: {fault_other}"
    else:
        fault_other = ""
        fault_name = ft["name"]

    priority = priority if priority in PRIORITY_KEYS else "medium"
    location_text = " — ".join(x for x in [building.strip(), floor.strip(), room.strip()] if x)

    resp_target = None
    resolve_target = None
    sla = (ft["sla_hours"] or db.sla_response_hours(priority))
    from datetime import timedelta
    resp_target = (datetime.now() + timedelta(hours=int(sla))).isoformat(timespec="seconds")
    resolve_target = (datetime.now() + timedelta(
        hours=int(db.sla_resolve_hours(priority)))).isoformat(timespec="seconds")

    try:
        cid = db.create_complaint({
            "reporter_name": reporter_name,
            "reporter_contact": reporter_contact,
            "reporter_email": reporter_email,
            "reporter_user_id": user["id"] if user else None,
            "department": department,
            "building": building,
            "floor": floor,
            "room": room,
            "location_text": location_text,
            "fault_type_id": ft["id"],
            "fault_type": fault_name,
            "fault_type_other": fault_other or None,
            "specialty_id": ft["specialty_id"],
            "specialty": ft["specialty_name"],
            "title": title,
            "description": description,
            "priority": priority,
            "affects_safety": affects_safety == "1",
            "affects_continuity": affects_continuity == "1",
            "extra_notes": extra_notes,
            "is_demo": is_demo == "1",
            "resp_target_at": resp_target,
            "resolve_target_at": resolve_target,
        })
    except Exception as exc:
        return render(request, "report_form.html", {
            "error": f"تعذر حفظ البلاغ: {exc}",
            "departments": db.list_departments(only_active=True),
            "fault_types": db.list_fault_types(only_active=True),
            "specialties": db.list_specialties(only_active=True),
        }, status_code=500)

    # Save attachments
    saved_attachments = []
    for f in attachments or []:
        try:
            rel = await _save_upload(f, prefix=f"cmp{cid}")
        except ValueError as e:
            rel = ""
        if rel:
            db.add_attachment(cid, f.filename or os.path.basename(rel), rel, f.content_type, 0)
            saved_attachments.append(rel)

    complaint = db.get_complaint(cid)

    # Real-time push to open dashboards
    post_report_hook(complaint)

    # Out-of-band notifications (email / telegram / whatsapp)
    notifier.notify_new_complaint(complaint)

    # Auto-export the live Excel snapshot on every new complaint
    save_live_excel()

    return render(request, "report_success.html", {
        "complaint": complaint,
        "track_url": track_url(request, complaint["ticket_no"]),
    })


@app.get("/track", response_class=HTMLResponse)
def track_go(request: Request, q: str = ""):
    if q.strip():
        return redirect(f"/track/{q.strip()}")
    return redirect("/")


@app.get("/track/{ticket_no}", response_class=HTMLResponse)
def track_complaint(request: Request, ticket_no: str):
    c = db.get_complaint_by_ticket(ticket_no)
    if c is None:
        return render(request, "404.html", {}, status_code=404)
    events = db.list_complaint_events(c["id"])
    return render(request, "track.html", {
        "c": c, "events": events,
        "track_url": track_url(request, c["ticket_no"]),
        "qr_src": f"/qr/{c['ticket_no']}.png",
    })


@app.get("/card/{ticket_no}", response_class=HTMLResponse)
def complaint_card(request: Request, ticket_no: str):
    """Public, print-optimised complaint card (with QR)."""
    c = db.get_complaint_by_ticket(ticket_no)
    if c is None:
        return render(request, "404.html", {}, status_code=404)
    return render(request, "card.html", {
        "c": c,
        "track_url": track_url(request, c["ticket_no"]),
        "qr_src": f"/qr/{c['ticket_no']}.png",
    })


@app.get("/qr/{ticket_no}.png")
def complaint_qr(request: Request, ticket_no: str):
    """Public QR image that points to the tracking page of a complaint."""
    c = db.get_complaint_by_ticket(ticket_no)
    if c is None:
        raise HTTPException(status_code=404)
    png = ex.make_qr_png(track_url(request, c["ticket_no"]))
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/form-qr.png")
def form_qr(request: Request):
    """Fixed QR that always points to the standalone public report form.

    Suitable for printing on stickers/posters so anyone can scan and submit a
    maintenance request directly, without ever reaching the dashboard.
    """
    png = ex.make_qr_png(f"{public_base(request)}/report", box_size=10, border=2)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/poster", response_class=HTMLResponse)
def poster(request: Request):
    """Printable A4 poster with the fixed form QR (public, RTL)."""
    return render(request, "poster.html", {
        "form_url": f"{public_base(request)}/report",
        "qr_src": "/form-qr.png",
    })


@app.get("/set-lang/{code}")
def set_lang(request: Request, code: str, next: str = "/"):
    code = code if code in i18n.LANGS else i18n.DEFAULT_LANG
    target = next if next.startswith("/") else "/"
    resp = redirect(target)
    resp.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
    return resp


# ---------------------------------------------------------------------------
# Dashboard & complaints (protected)
# ---------------------------------------------------------------------------

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, page: int = Query(1), status: str = "all",
              priority: str = "all", department: str = "all", specialty: str = "all",
              q: str = "", date_from: str = "", date_to: str = "", demo: str = "real"):
    user = admin_required(request)
    filters = {
        "status": status, "priority": priority, "department": department,
        "specialty": specialty, "q": q, "date_from": date_from, "date_to": date_to,
        "demo": demo,
    }
    per_page = 50
    total = db.count_complaints(filters)
    pages = max(1, -(-total // per_page))
    page = min(max(page, 1), pages)
    complaints = db.list_complaints(filters, limit=per_page, offset=(page - 1) * per_page,
                                    order="priority_first")
    st = db.stats(filters)
    return render(request, "dashboard.html", {
        "complaints": complaints,
        "stats": st,
        "filters": filters,
        "page": page,
        "pages": pages,
        "total": total,
        "departments": db.list_departments(only_active=True),
        "specialties": db.list_specialties(only_active=True),
        "overdue": db.overdue_complaints(limit=20),
        "chart_departments": db.complaints_by_department(),
        "chart_types": db.complaints_by_fault_type(),
        "chart_priority": db.complaints_by_priority(),
        "over_time": db.complaints_over_time(30),
        "avg_response": db.avg_response_hours(),
        "avg_resolve": db.avg_resolve_hours(),
        "tech_perf": db.technicians_performance(),
        "form_url": f"{public_base(request)}/report",
        "form_qr": "/form-qr.png",
    })


@app.get("/complaints/{cid}", response_class=HTMLResponse)
def complaint_detail(request: Request, cid: int):
    user = admin_required(request)
    c = db.get_complaint(cid)
    if c is None:
        return render(request, "404.html", {}, status_code=404)
    # staff viewers can only see... all roles can view list & details except viewer-limited
    return render(request, "complaint_detail.html", {
        "c": c,
        "events": db.list_complaint_events(cid),
        "notes": db.list_internal_notes(cid),
        "attachments": db.list_attachments(cid),
        "technicians": db.list_technicians(only_active=True),
        "specialties": db.list_specialties(only_active=True),
        "departments": db.list_departments(only_active=True),
        "fault_types": db.list_fault_types(only_active=True),
        "can_manage": user_can_manage(user),
        "is_admin_area": user_can_admin(user),
        "track_url": track_url(request, c["ticket_no"]),
        "qr_src": f"/qr/{c['ticket_no']}.png",
    })


@app.get("/complaints/{cid}/pdf")
def complaint_pdf(request: Request, cid: int):
    admin_required(request)
    c = db.get_complaint(cid)
    if c is None:
        raise HTTPException(404)
    data = ex.build_complaint_pdf(c, track_url(request, c["ticket_no"]),
                                  events=db.list_complaint_events(cid))
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="{c["ticket_no"]}.pdf"'})


@app.post("/complaints/{cid}/status")
def complaint_set_status(request: Request, cid: int, status: str = Form(...),
                         cancel_reason: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user) and status in ("closed", "verified"):
        raise HTTPException(status_code=403)
    c = db.get_complaint(cid)
    if c is None:
        raise HTTPException(404)
    fields = {"status": status}
    if status == db.CANCELLED:
        fields["cancel_reason"] = cancel_reason
    events = db.update_complaint(cid, fields, actor=user["full_name"])
    if events:
        post_report_hook(db.get_complaint(cid), event="status_change")
    c = db.get_complaint(cid)
    notifier.notify_status_change(c)
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/assign")
def complaint_assign(request: Request, cid: int, technician: str = Form(""),
                     team: str = Form(""), specialty: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    fields = {}
    if technician:
        fields["assigned_technician"] = technician
    if team:
        fields["assigned_team"] = team
    if specialty:
        fields["specialty"] = specialty
    db.update_complaint(cid, fields, actor=user["full_name"])
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/note")
def complaint_note(request: Request, cid: int, note: str = Form(...)):
    user = admin_required(request)
    if not note.strip():
        return redirect(f"/complaints/{cid}")
    db.add_internal_note(cid, user["full_name"], note)
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/comment")
def complaint_comment(request: Request, cid: int, comment: str = Form(...)):
    """Public comment recorded on the timeline (reporter-facing)."""
    user = admin_required(request)
    if not comment.strip():
        return redirect(f"/complaints/{cid}")
    db.add_internal_note(cid, f"{user['full_name']} (تعليق)", comment)
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/edit")
def complaint_edit(request: Request, cid: int, title: str = Form(""),
                   description: str = Form(""), priority: str = Form(""),
                   department: str = Form(""), extra_notes: str = Form(""),
                   fault_type_id: str = Form(""), fault_type_other: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    fields = {}
    if title:
        fields["title"] = title
    if description:
        fields["description"] = description
    if priority and priority in PRIORITY_KEYS:
        fields["priority"] = priority
    if department:
        fields["department"] = department
    if extra_notes:
        fields["extra_notes"] = extra_notes
    if fault_type_id.strip().isdigit():
        tid = int(fault_type_id)
        for row in db.list_fault_types():
            if row["id"] == tid:
                other = fault_type_other.strip()
                if db.is_other_fault_name(row["name"]):
                    fields["fault_type"] = f"{row['name']}: {other}" if other else row["name"]
                    fields["fault_type_other"] = other
                else:
                    fields["fault_type"] = row["name"]
                    fields["fault_type_other"] = ""
                break
    db.update_complaint(cid, fields, actor=user["full_name"])
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/attachments")
async def complaint_upload(request: Request, cid: int, file: UploadFile = File(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    rel = await _save_upload(file, prefix=f"cmp{cid}")
    if rel:
        db.add_attachment(cid, file.filename or os.path.basename(rel), rel, file.content_type or "", 0)
    return redirect(f"/complaints/{cid}")


@app.post("/complaints/{cid}/attachments/{aid}/delete")
def complaint_attachment_delete(request: Request, cid: int, aid: int):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    att = db.get_attachment(aid)
    if att:
        try:
            p = BASE_DIR / att["stored"]
            if p.exists():
                p.unlink()
        except Exception:
            pass
        db.delete_attachment(aid)
    return redirect(f"/complaints/{cid}")


# ---------------------------------------------------------------------------
# Settings / administration
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tab: str = "departments"):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    return render(request, "settings.html", {
        "tab": tab,
        "departments": db.list_departments(),
        "specialties": db.list_specialties(),
        "fault_types": db.list_fault_types(),
        "technicians": db.list_technicians(),
        "users": db.list_users(),
        "sla": {p: (db.sla_response_hours(p), db.sla_resolve_hours(p))
                for p, _, _ in db.PRIORITIES},
        "is_admin_area": user_can_admin(user),
        "notif_log": db.list_notifications(60),
        "hospital_name": db.get_setting("hospital_name", ""),
    })


@app.post("/settings/departments/add")
def settings_add_department(request: Request, name: str = Form(...), building: str = Form(""),
                            floor: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    if name.strip():
        db.add_department(name, building, floor)
    return redirect("/settings?tab=departments")


@app.post("/settings/departments/update")
def settings_update_department(request: Request, id: int = Form(...),
                               name: str = Form(""), active: str = Form("1")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.update_department(id, {"name": name, "active": 1 if active == "1" else 0})
    return redirect("/settings?tab=departments")


@app.post("/settings/departments/delete")
def settings_delete_department(request: Request, id: int = Form(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.delete_department(id)
    return redirect("/settings?tab=departments")


@app.post("/settings/specialties/add")
def settings_add_specialty(request: Request, name: str = Form(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    if name.strip():
        db.add_specialty(name)
    return redirect("/settings?tab=specialties")


@app.post("/settings/specialties/update")
def settings_update_specialty(request: Request, id: int = Form(...),
                              name: str = Form(""), active: str = Form("1")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.update_specialty(id, name=name, active=1 if active == "1" else 0)
    return redirect("/settings?tab=specialties")


@app.post("/settings/specialties/delete")
def settings_delete_specialty(request: Request, id: int = Form(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.delete_specialty(id)
    return redirect("/settings?tab=specialties")


@app.post("/settings/faults/add")
def settings_add_fault(request: Request, name: str = Form(...),
                       specialty_id: str = Form(""), priority: str = Form("medium"),
                       sla_hours: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    if name.strip():
        db.add_fault_type(name,
                          int(specialty_id) if specialty_id.strip() else None,
                          priority, sla_hours or None)
    return redirect("/settings?tab=faults")


@app.post("/settings/faults/update")
def settings_update_fault(request: Request, id: int = Form(...), name: str = Form(""),
                          specialty_id: str = Form(""), priority: str = Form("medium"),
                          sla_hours: str = Form(""), active: str = Form("1")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.update_fault_type(id, {
        "name": name,
        "specialty_id": specialty_id or None,
        "priority": priority,
        "sla_hours": sla_hours or None,
        "active": 1 if active == "1" else 0,
    })
    return redirect("/settings?tab=faults")


@app.post("/settings/faults/delete")
def settings_delete_fault(request: Request, id: int = Form(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.delete_fault_type(id)
    return redirect("/settings?tab=faults")


@app.post("/settings/technicians/add")
def settings_add_technician(request: Request, name: str = Form(...),
                            specialty_id: str = Form(""), phone: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    if name.strip():
        db.add_technician(name, int(specialty_id) if specialty_id.strip() else None, phone)
    return redirect("/settings?tab=technicians")


@app.post("/settings/technicians/update")
def settings_update_technician(request: Request, id: int = Form(...), name: str = Form(""),
                               specialty_id: str = Form(""), phone: str = Form(""),
                               active: str = Form("1")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.update_technician(id, {"name": name, "specialty_id": specialty_id or None,
                              "phone": phone, "active": 1 if active == "1" else 0})
    return redirect("/settings?tab=technicians")


@app.post("/settings/technicians/delete")
def settings_delete_technician(request: Request, id: int = Form(...)):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    db.delete_technician(id)
    return redirect("/settings?tab=technicians")


@app.post("/settings/users/add")
def settings_add_user(request: Request, username: str = Form(...), password: str = Form(...),
                      full_name: str = Form(...), role: str = Form("staff"),
                      department: str = Form(""), phone: str = Form("")):
    user = admin_required(request)
    if not user_can_admin(user):
        raise HTTPException(403)
    if username.strip() and password and full_name.strip():
        try:
            db.create_user(username.strip(), password, full_name.strip(), role,
                           department.strip() or None, phone.strip() or None)
        except Exception as exc:
            return render(request, "login.html", {"error": f"تعذر إنشاء المستخدم: {exc}"})
    return redirect("/settings?tab=users")


@app.post("/settings/users/update")
def settings_update_user(request: Request, id: int = Form(...), full_name: str = Form(""),
                         role: str = Form("staff"), department: str = Form(""),
                         phone: str = Form(""), active: str = Form("1")):
    user = admin_required(request)
    if not user_can_admin(user):
        raise HTTPException(403)
    db.update_user(id, {"full_name": full_name, "role": role, "department": department,
                        "phone": phone, "active": 1 if active == "1" else 0})
    return redirect("/settings?tab=users")


@app.post("/settings/users/password")
def settings_user_password(request: Request, id: int = Form(...), password: str = Form(...)):
    user = admin_required(request)
    if not user_can_admin(user):
        raise HTTPException(403)
    if password.strip():
        db.set_user_password(id, password)
    return redirect("/settings?tab=users")


@app.post("/settings/sla")
async def settings_sla(request: Request):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    form = await request.form()
    for p, _, _ in db.PRIORITIES:
        r = form.get(f"resp_{p}", "")
        s = form.get(f"res_{p}", "")
        if r.strip().isdigit():
            db.set_setting(f"sla_response_{p}", r.strip())
        if s.strip().isdigit():
            db.set_setting(f"sla_resolve_{p}", s.strip())
    return redirect("/settings?tab=sla")


@app.post("/settings/general")
def settings_general(request: Request, hospital_name: str = Form("")):
    user = admin_required(request)
    if not user_can_manage(user):
        raise HTTPException(403)
    if hospital_name.strip():
        db.set_setting("hospital_name", hospital_name.strip())
    return redirect("/settings?tab=general")


@app.post("/settings/reset")
def settings_reset(request: Request, confirm: str = Form("")):
    """Admin-only: wipe every complaint and restart numbering from 00001."""
    user = admin_required(request)
    if confirm.strip() != "تصفير":
        return redirect("/settings?tab=general&reset=badword")
    result = db.reset_complaints()
    # Remove stored attachment files from disk
    for stored in result.get("stored", []):
        try:
            rel = stored.lstrip("/").replace("/", os.sep)
            p = BASE_DIR / rel
            if p.exists():
                p.unlink()
        except Exception:
            pass
    try:
        if LIVE_EXCEL_PATH.exists():
            LIVE_EXCEL_PATH.unlink()
    except Exception:
        pass
    save_live_excel()
    return redirect("/settings?tab=general&reset=done")


# ---------------------------------------------------------------------------
# Reports / analytics / exports
# ---------------------------------------------------------------------------

@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, status: str = "all", priority: str = "all",
                 department: str = "all", date_from: str = "", date_to: str = "",
                 q: str = ""):
    user = admin_required(request)
    filters = {"status": status, "priority": priority, "department": department,
               "date_from": date_from, "date_to": date_to, "q": q, "specialty": "all",
               "demo": "real"}
    rows = db.report_rows(filters)
    return render(request, "reports.html", {
        "filters": filters,
        "rows": rows,
        "count": len(rows),
        "departments": db.list_departments(only_active=True),
        "by_department": db.complaints_by_department(),
        "by_type": db.complaints_by_fault_type(),
        "tech_perf": db.technicians_performance(),
        "avg_response": db.avg_response_hours(),
        "avg_resolve": db.avg_resolve_hours(),
        "recurring": db.recurring_locations(),
        "over_time": db.complaints_over_time(90),
        "stats": db.stats({"demo": "real"}),
    })


@app.get("/reports/export/excel")
def reports_export_excel(request: Request, status: str = "all", priority: str = "all",
                         department: str = "all", date_from: str = "", date_to: str = "", q: str = ""):
    user = admin_required(request)
    filters = {"status": status, "priority": priority, "department": department,
               "date_from": date_from, "date_to": date_to, "q": q, "specialty": "all",
               "demo": "real"}
    data = ex.build_excel(filters)
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="maintenance-report-{datetime.now():%Y%m%d}.xlsx"'})


@app.get("/reports/export/pdf")
def reports_export_pdf(request: Request, status: str = "all", priority: str = "all",
                       department: str = "all", date_from: str = "", date_to: str = "", q: str = ""):
    user = admin_required(request)
    filters = {"status": status, "priority": priority, "department": department,
               "date_from": date_from, "date_to": date_to, "q": q, "specialty": "all",
               "demo": "real"}
    data = ex.build_pdf(filters)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="maintenance-report-{datetime.now():%Y%m%d}.pdf"'})


@app.get("/reports/export/live")
def reports_export_live(request: Request):
    """Always-fresh hierarchical Excel (grouped by department)."""
    admin_required(request)
    data = ex.build_live_excel({"demo": "all"})
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="maintenance-live-tree-{datetime.now():%Y%m%d}.xlsx"'})


@app.get("/api/stats")
def api_stats(request: Request):
    user = admin_required(request)
    return JSONResponse(db.stats({"demo": "real"}))


@app.get("/api/complaints/latest")
def api_latest(request: Request, since: str = Query("")):
    user = admin_required(request)
    rows = db.list_complaints({"status": "all", "priority": "all", "department": "all",
                               "specialty": "all", "q": "", "demo": "real"},
                              limit=10, order="recency")
    return JSONResponse([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return JSONResponse({"status": "ok", "db": "pg" if db._db.is_pg() else "sqlite"})


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)