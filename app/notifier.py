"""Notifications for the Hospital Maintenance complaint system.

Channels (all optional; skipped silently if not configured):

1. Email  - SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / NOTIFY_TO
2. Telegram - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (fast, free, reaches the
   phone instantly via the Telegram app).
3. WhatsApp (Meta Cloud API) - WHATSAPP_TOKEN / WHATSAPP_PHONE_ID / WHATSAPP_TO

Every attempt is also recorded in the notification_log table so delivery
history is visible in the dashboard (nothing silently loses data).
"""

import json
import os
import smtplib
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import database as db


def _cfg(key: str) -> str:
    return os.environ.get(key, "").strip()


def _app_base_url() -> str:
    return _cfg("BASE_URL")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def email_enabled() -> bool:
    return bool(_cfg("SMTP_USER") and _cfg("SMTP_PASSWORD") and _cfg("NOTIFY_TO"))


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    """Send one email. Returns True on success, False on any failure."""
    if not email_enabled():
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _cfg("SMTP_USER")
    msg["To"] = to
    part_text = MIMEText(body, "plain", "utf-8")
    msg.attach(part_text)
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        server = smtplib.SMTP(
            _cfg("SMTP_HOST") or "smtp.gmail.com",
            int(_cfg("SMTP_PORT") or 587),
            timeout=20,
        )
        server.starttls()
        server.login(_cfg("SMTP_USER"), _cfg("SMTP_PASSWORD"))
        server.sendmail(_cfg("SMTP_USER"), [to], msg.as_string())
        server.quit()
        return True
    except Exception as exc:
        try:
            db.log_notification(None, "email", to, subject, "failed", str(exc))
        except Exception:
            pass
        return False


def _status_map() -> dict:
    return dict((s, ar) for s, ar, _ in db.STATUSES + [(db.CANCELLED, "ملغي", "")])


def _complaint_plain(c) -> str:
    status_ar = _status_map().get(c["status"], c["status"])
    safety = "نعم" if c["affects_safety"] else "لا"
    cont = "نعم" if c["affects_continuity"] else "لا"
    return f"""بلاغ صيانة جديد - تقرير فوري

رقم البلاغ: {c['ticket_no']}
العنوان: {c['title']}
القسم: {c['department']}
الموقع: {c['location_text'] or '-'}
نوع العطل: {c['fault_type']}
التخصص: {c['specialty'] or '-'}
الأولوية: {c['priority']}
يؤثر على سلامة المرضى: {safety}
يؤثر على استمرارية العمل: {cont}

الوصف:
{c['description']}

مقدم البلاغ: {c['reporter_name']} ({c['reporter_contact']})
وقت الإرسال: {datetime.now().strftime('%Y/%m/%d %H:%M')}
"""


def notify_new_complaint(c) -> list:
    """Fire all configured notification channels for a new complaint.

    Returns a list of (channel, recipient) pairs that were actually sent so
    the caller can record them in the notification log.
    """
    sent = []
    subject = f"[بلاغ جديد] {c['ticket_no']} - {c['fault_type']}"
    text = _complaint_plain(c)
    base = _app_base_url()

    if email_enabled():
        link = f"{base}/complaints/{c['id']}" if base else ""
        html = (_complaint_html(c, link) if link else None)
        if send_email(_cfg("NOTIFY_TO"), subject, text, html):
            sent.append(("email", _cfg("NOTIFY_TO")))
        else:
            try:
                db.log_notification(c["id"], "email", _cfg("NOTIFY_TO"), subject, "failed", "smtp error")
            except Exception:
                pass

    if _cfg("TELEGRAM_BOT_TOKEN") and _cfg("TELEGRAM_CHAT_ID"):
        if _send_telegram(_cfg("TELEGRAM_CHAT_ID"), _telegram_text(c, subject)):
            sent.append(("telegram", _cfg("TELEGRAM_CHAT_ID")))
        else:
            try:
                db.log_notification(c["id"], "telegram", _cfg("TELEGRAM_CHAT_ID"), subject, "failed", "telegram error")
            except Exception:
                pass

    if _cfg("WHATSAPP_TOKEN") and _cfg("WHATSAPP_PHONE_ID") and _cfg("WHATSAPP_TO"):
        if _send_whatsapp(_cfg("WHATSAPP_TO"), _whatsapp_text(c)):
            sent.append(("whatsapp", _cfg("WHATSAPP_TO")))
        else:
            try:
                db.log_notification(c["id"], "whatsapp", _cfg("WHATSAPP_TO"), subject, "failed", "whatsapp error")
            except Exception:
                pass

    # Record in log
    for channel, recip in sent:
        try:
            db.log_notification(c["id"], channel, recip, subject, "sent")
        except Exception:
            pass
    return sent


def notify_status_change(c) -> None:
    """Optional courtesy email to the reporter when their complaint status changes."""
    if not email_enabled() or not c.get("reporter_email"):
        return
    status_ar = _status_map().get(c["status"], c["status"])
    subject = f"تحديث بلاغ الصيانة {c['ticket_no']} — {status_ar}"
    body = f"""السادة/ مقدم البلاغ {c['reporter_name']},

نود إعلامكم أن بلاغ الصيانة رقم {c['ticket_no']} أصبح بحالة: {status_ar}.

تفاصيل: {c['title']}
شكراً لتعاونكم - قسم الصيانة والتشغيل
"""
    send_email(c["reporter_email"], subject, body)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _telegram_text(c, subject) -> str:
    return f"""🚨 *بلاغ صيانة جديد*

*{subject}*

🏥 القسم: {c['department']}
📍 الموقع: {c['location_text'] or '-'}
🔧 نوع العطل: {c['fault_type']}
👨‍🔧 التخصص: {c['specialty'] or '-'}
⚡ الأولوية: {c['priority']}
🕐 الوقت: {datetime.now().strftime('%Y/%m/%d %H:%M')}

{c['description'][:400]}

— {c['reporter_name']} ({c['reporter_contact']})
"""


def _send_telegram(chat_id: str, text: str) -> bool:
    token = _cfg("TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------

def _whatsapp_text(c) -> str:
    return (
        f"بلاغ صيانة جديد 🔧\n"
        f"الرقم: {c['ticket_no']}\n"
        f"القسم: {c['department']}\n"
        f"الموقف: {c['location_text'] or '-'}\n"
        f"النوع: {c['fault_type']}\n"
        f"الأولوية: {c['priority']}\n"
        f"المُبلّغ: {c['reporter_name']} ({c['reporter_contact']})\n"
        f"الوصف: {c['description'][:150]}"
    )


def _send_whatsapp(to: str, body: str) -> bool:
    token = _cfg("WHATSAPP_TOKEN")
    phone_id = _cfg("WHATSAPP_PHONE_ID")
    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HTML email
# ---------------------------------------------------------------------------

def _complaint_html(c, link) -> str:
    status_ar = _status_map().get(c["status"], c["status"])
    colors = dict((pr, cl) for pr, _, cl in db.PRIORITIES)
    return f"""<div dir="rtl" style="font-family:Tahoma,Arial;max-width:560px;margin:auto;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
<table width="100%" style="background:#0b6b42;color:#fff;padding:16px;font-size:18px"><tr><td>🛠️ بلاغ صيانة جديد — إشعار فوري</td></tr></table>
<div style="padding:18px">
<p><b>رقم البلاغ:</b> <span style="color:#0b6b42;font-weight:bold">{c['ticket_no']}</span></p>
<p><b>القسم:</b> {c['department']} {('- ' + c['location_text']) if c.get('location_text') else ''}</p>
<p><b>نوع العطل:</b> {c['fault_type']} {('- ' + str(c['specialty']) + ' (تخصص)') if c.get('specialty') else ''}</p>
<p><b>الأولوية:</b> <span style="color:{colors.get(c['priority'],'#334155')};font-weight:bold">{c['priority']}</span></p>
<p><b>يؤثر على سلامة المرضى:</b> {'نعم ⚠️' if c['affects_safety'] else 'لا'}</p>
<p><b>الوصف:</b><br/>{c['description']}</p>
<p><b>مقدم البلاغ:</b> {c['reporter_name']} ({c['reporter_contact']})</p>
<p style="margin-top:16px"><a href="{link}" style="background:#0b6b42;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none">عرض البلاغ في لوحة التحكم</a></p>
</div></div>"""


def config_status(lang: str = "ar") -> list:
    """Human-readable summary of configured channels (for the settings page)."""
    from . import i18n

    def tr(key: str) -> str:
        return i18n.translate(key, lang)

    email_ok = email_enabled()
    tg_ok = bool(_cfg("TELEGRAM_BOT_TOKEN") and _cfg("TELEGRAM_CHAT_ID"))
    wa_ok = bool(_cfg("WHATSAPP_TOKEN") and _cfg("WHATSAPP_PHONE_ID") and _cfg("WHATSAPP_TO"))
    return [
        {"name": tr("ch.email.name"),
         "configured": email_ok,
         "detail": (_cfg("NOTIFY_TO") if email_ok else tr("ch.email.missing"))},
        {"name": tr("ch.telegram.name"),
         "configured": tg_ok,
         "detail": (_cfg("TELEGRAM_CHAT_ID") if tg_ok else tr("ch.telegram.missing"))},
        {"name": tr("ch.whatsapp.name"),
         "configured": wa_ok,
         "detail": (_cfg("WHATSAPP_TO") if wa_ok else tr("ch.whatsapp.missing"))},
        {"name": tr("ch.sse.name"),
         "configured": True,
         "detail": tr("ch.sse.detail")},
    ]