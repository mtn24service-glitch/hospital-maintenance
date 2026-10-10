"""Seed reference data + a default demo admin (run once).

    python -m app.seed

Safe to re-run: it skips anything that already exists. Demo complaints are
flagged is_demo=1 so they never mix with real operational data in dashboards.
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import database as db  # noqa: E402

db.init_db()
db.ensure_first_admin()

# ---------------------------------------------------------------------------
# Hospital departments (buildings / floors / rooms are free-text on the form
# and can be added from the settings page too).
# ---------------------------------------------------------------------------

DEPARTMENTS = [
    ("طوارئ", "المبنى الرئيسي", "الدور الأرضي"),
    ("العناية المركزة", "المبنى الرئيسي", "الدور الأول"),
    ("العناية المركزة - حديثي الولادة", "المبنى الرئيسي", "الدور الأول"),
    ("غرف العمليات والإفاقة", "المبنى الرئيسي", "الدور الثاني"),
    ("قسم التنويم - رجال", "المبنى الرئيسي", "الدور الثاني والثالث"),
    ("قسم التنويم - نساء", "المبنى الرئيسي", "الدور الرابع"),
    ("العيادات الخارجية", "مبنى العيادات", "جميع الأدوار"),
    ("المختبر", "المبنى الرئيسي", "الدور الأرضي"),
    ("الأشعة والتصوير الطبي", "المبنى الرئيسي", "الدور الأرضي"),
    ("الصيدلية", "المبنى الرئيسي", "الدور الأرضي"),
    ("المستودعات", "المبنى الرئيسي", "الدور السفلي"),
    ("الإدارة والمكاتب الإدارية", "المبنى الإداري", "جميع الأدوار"),
    ("الخدمات العامة والممرات", "جميع المباني", "جميع الأدوار"),
    ("دورات المياه - عيادات", "مبنى العيادات", "جميع الأدوار"),
    ("الغسيل والتكييف المركزي", "مبنى الخدمات", "جميع الأدوار"),
    ("المطبخ الرئيسي", "مبنى الخدمات", "الدور الأرضي"),
    ("مواقف السيارات والأمن", "المبنى الرئيسي", "الدور السفلي والخارج"),
    ("الغازات الطبية", "جميع المباني", "جميع الأدوار"),
]

for name, building, floor in DEPARTMENTS:
    try:
        db.add_department(name, building, floor)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Specialties (responsible trades)
# ---------------------------------------------------------------------------

SPECIALTIES = [
    "كهرباء", "تكييف وتبريد", "سباكة", "نجارة وحدادة", "ألمنيوم وزجاج",
    "طلاء ودهانات", "أنظمة الحريق والسلامة", "مصاعد", "أجهزة طبية",
    "غازات طبية", "شبكات واتصالات", "أعمال مدنية",
]

for s in SPECIALTIES:
    try:
        db.add_specialty(s)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Fault types -> specialty mapping (editable later from the settings page)
# ---------------------------------------------------------------------------

FAULTS = [
    ("كهرباء - انقطاع التيار", "كهرباء", "critical", 1),
    ("إنارة معطلة", "كهرباء", "low", 72),
    ("مخرج كهرباء معطل", "كهرباء", "medium", 24),
    ("تكييف لا يعمل", "تكييف وتبريد", "high", 4),
    ("تسريب ماء", "سباكة", "high", 4),
    ("صرف صحي مسدود", "سباكة", "high", 4),
    ("تسريب تحت الحوض", "سباكة", "medium", 24),
    ("باب لا يغلق", "نجارة وحدادة", "low", 72),
    ("قفل تالف", "نجارة وحدادة", "medium", 24),
    ("نافذة مكسورة", "ألمنيوم وزجاج", "medium", 24),
    ("تشقق جدار / سقف", "أعمال مدنية", "medium", 24),
    ("تلف أرضية", "أعمال مدنية", "medium", 24),
    ("طلاء متقشر", "طلاء ودهانات", "low", 120),
    ("مصعد متوقف", "مصاعد", "critical", 1),
    ("إنذار حريق خاطئ", "أنظمة الحريق والسلامة", "high", 4),
    ("طفاية حريق غير صالحة", "أنظمة الحريق والسلامة", "high", 4),
    ("جهاز طبي عطل", "أجهزة طبية", "critical", 1),
    ("غاز طبي - تسريب", "غازات طبية", "critical", 1),
    ("ضعف شبكة المياه", "كهرباء", "high", 4),  # electrical pumps; adjust as needed
    ("أخرى", None, "medium", 24),
]

specialty_map = {}
for s in db.list_specialties():
    specialty_map[s["name"]] = s["id"]

for name, spec_name, priority, sla in FAULTS:
    try:
        db.add_fault_type(name, specialty_map.get(spec_name), priority, sla)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Demo technicians
# ---------------------------------------------------------------------------

TECHNICIANS = [
    ("فهد العمري", "كهرباء"),
    ("سالم القرني", "تكييف وتبريد"),
    ("محمد شوقي", "سباكة"),
    ("خالد مبارك", "نجارة وحدادة"),
    ("عبدالله حسن", "أعمال مدنية"),
    ("ياسر عسيري", "مصاعد"),
    ("حسن راضي", "أنظمة الحريق والسلامة"),
    ("أحمد الطيب", "أجهزة طبية"),
]

for name, spec_name in TECHNICIANS:
    try:
        db.add_technician(name, specialty_map.get(spec_name))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Demo staff/test users (separate from real data - password: demo1234)
# ---------------------------------------------------------------------------

if db.get_user_by_username("demo_staff") is None:
    db.create_user("demo_staff", "demo1234", "موظف تجريبي (موظف قسم)",
                   "staff", "العيادات الخارجية")
if db.get_user_by_username("tech_demo") is None:
    db.create_user("tech_demo", "demo1234", "فني تجريبي", "technician", "قسم الصيانة")
if db.get_user_by_username("manager_demo") is None:
    db.create_user("manager_demo", "demo1234", "مسؤول صيانة تجريبي",
                   "maint_manager", "قسم الصيانة")

print("Seed complete:")
print("  - Departments:", len(DEPARTMENTS))
print("  - Specialties:", len(SPECIALTIES))
print("  - Fault types:", len(FAULTS))
print("  - Technicians:", len(TECHNICIANS))
print("  - Users: admin/admin123 (مدير النظام) و demo_staff/demo1234 و manager_demo/demo1234")
print("System is ready.")