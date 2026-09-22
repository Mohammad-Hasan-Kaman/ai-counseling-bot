# -*- coding: utf-8 -*-
"""
ماژول مدیریت دیتابیس مراجعین و ثبت سوابق مشاوره‌ها
مرکز مشاوره خانواده نیک‌روان
"""
import sqlite3
import json
from pathlib import Path
from config import USER_RECORDS_DB

def init_user_db():
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()

    # جدول مشخصات مراجعین
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        phone TEXT PRIMARY KEY,
        full_name TEXT NOT NULL,
        first_telegram_id INTEGER,
        request_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # جدول سوابق درخواست‌ها و نتایج
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        phone TEXT,
        full_name TEXT,
        gender TEXT,
        age INTEGER,
        topic TEXT,
        has_prev_therapy INTEGER,
        prev_detail TEXT,
        expectation TEXT,
        preferred_gender TEXT,
        branch TEXT,
        ghq_scores TEXT,
        ghq_total INTEGER,
        recommendations TEXT,
        request_number INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()

    # مایگریشن: افزودن ستون branch به جدول موجود (در صورت نبود)
    existing_cols = [c[1] for c in cur.execute("PRAGMA table_info(user_requests)").fetchall()]
    if "branch" not in existing_cols:
        cur.execute("ALTER TABLE user_requests ADD COLUMN branch TEXT")

    conn.commit()
    conn.close()


def init_consultants_db():
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS consultants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        ability REAL DEFAULT 2.0,
        location TEXT DEFAULT 'هر دو',
        education_experience TEXT,
        general_area TEXT,
        general_area_2 TEXT,
        specializations TEXT,
        detailed_topics TEXT,
        age_range TEXT,
        license TEXT,
        notes TEXT,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS upload_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        consultant_count INTEGER,
        file_size_bytes INTEGER,
        uploaded_by INTEGER,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()


def replace_consultants(profiles: list, uploaded_by: int = 0) -> dict:
    """
    جایگزینی کامل دیتای مشاورین: داده قدیمی حذف و لیست جدید درج می‌شود.
    خروجی: آمار مقایسه‌ای با دیتای قبلی برای گزارش
    """
    import json as _json
    init_consultants_db()
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()
    try:
        # دیتای قبل از حذف برای گزارش مقایسه‌ای
        old_names = {r[0] for r in cur.execute("SELECT name FROM consultants").fetchall()}
        old_count = len(old_names)

        cur.execute("DELETE FROM consultants")
        inserted = 0
        for p in profiles:
            cur.execute("""
                INSERT OR REPLACE INTO consultants
                (name, ability, location, education_experience, general_area,
                 general_area_2, specializations, detailed_topics, age_range, license, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("name", ""), p.get("ability", 2.0), p.get("location", "هر دو"),
                p.get("education_experience", ""), p.get("general_area", ""),
                p.get("general_area_2", ""), _json.dumps(p.get("specializations", {}), ensure_ascii=False),
                p.get("detailed_topics", ""), p.get("age_range", ""),
                p.get("license", ""), p.get("notes", ""),
            ))
            inserted += 1

        new_names = {p.get("name", "") for p in profiles}
        added = new_names - old_names
        removed = old_names - new_names

        cur.execute(
            "INSERT INTO upload_history (consultant_count, uploaded_by) VALUES (?, ?)",
            (len(profiles), uploaded_by),
        )
        conn.commit()
        return {
            "old_count": old_count,
            "new_count": len(profiles),
            "inserted": inserted,
            "added": sorted(added),
            "removed": sorted(removed),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_consultants_stats() -> dict:
    """آمار فیلدهای پرشده جدول مشاورین برای گزارش صحت داده"""
    import json as _json
    init_consultants_db()
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM consultants").fetchone()[0]

    def _filled(col):
        return cur.execute(
            f"SELECT COUNT(*) FROM consultants WHERE {col} IS NOT NULL AND TRIM({col}) != ''"
        ).fetchone()[0]

    stats = {
        "total": total,
        "with_ability": _filled("ability"),
        "with_location": _filled("location"),
        "with_education": _filled("education_experience"),
        "with_general_area": _filled("general_area"),
        "with_age_range": _filled("age_range"),
        "with_license": _filled("license"),
        "with_notes": _filled("notes"),
        "with_specializations": 0,
        "spec_total": 0,
        "by_location": {},
    }

    for loc, cnt in cur.execute("SELECT location, COUNT(*) FROM consultants GROUP BY location"):
        stats["by_location"][loc or "-"] = cnt

    for (spec_json,) in cur.execute("SELECT specializations FROM consultants").fetchall():
        try:
            spec = _json.loads(spec_json) if spec_json else {}
            if isinstance(spec, dict) and spec:
                stats["with_specializations"] += 1
                stats["spec_total"] += len(spec)
        except Exception:
            pass

    conn.close()
    return stats


def validate_phone_and_get_count(phone: str, input_name: str, telegram_id: int = 0) -> tuple[bool, int, str]:
    """
    اعتبارسنجی عدم تداخل شماره با نام دیگر و محاسبه مرتبه مراجعه
    خروجی: (مجاز_بودن, شماره_مراجعه, نام_ثبت_شده)
    """
    init_user_db()
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()
    cur.execute("SELECT full_name, request_count FROM users WHERE phone=?", (phone,))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        return True, 1, input_name
        
    stored_name, current_count = row
    norm_stored = " ".join(stored_name.strip().split()).lower()
    norm_input = " ".join(input_name.strip().split()).lower()
    
    # اگر شماره قبلاً با نام متفاوتی ثبت شده باشد
    if norm_stored != norm_input:
        return False, current_count, stored_name
        
    return True, current_count + 1, stored_name


def save_user_consultation(telegram_id: int, user_data: dict, recommendations: list) -> int:
    """ثبت سابقه و ارتقای شمارنده مراجعات"""
    phone = user_data.get("phone", "")
    full_name = user_data.get("full_name", "")
    
    init_user_db()
    conn = sqlite3.connect(str(USER_RECORDS_DB))
    cur = conn.cursor()
    
    cur.execute("SELECT request_count FROM users WHERE phone=?", (phone,))
    row = cur.fetchone()
    if row:
        req_number = row[0] + 1
        cur.execute("UPDATE users SET request_count=?, first_telegram_id=?, last_seen=CURRENT_TIMESTAMP WHERE phone=?", (req_number, telegram_id, phone))
    else:
        req_number = 1
        cur.execute("INSERT INTO users (phone, full_name, first_telegram_id, request_count) VALUES (?, ?, ?, 1)",
                    (phone, full_name, telegram_id))
                    
    ghq = user_data.get("ghq_scores")
    ghq_total = ghq.get("total") if (ghq and isinstance(ghq, dict)) else None
    ghq_json = json.dumps(ghq, ensure_ascii=False) if ghq else None
    recs_json = json.dumps(recommendations, ensure_ascii=False)
    
    cur.execute("""
    INSERT INTO user_requests (
        telegram_id, phone, full_name, gender, age, topic,
        has_prev_therapy, prev_detail, expectation, preferred_gender,
        branch, ghq_scores, ghq_total, recommendations, request_number
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id, phone, full_name, user_data.get("gender"), user_data.get("age"),
        user_data.get("topic"), 1 if user_data.get("has_prev_therapy") else 0,
        user_data.get("prev_detail", ""), user_data.get("expectation", ""),
        user_data.get("preferred_gender", ""), user_data.get("branch", "اهمیتی ندارد"),
        ghq_json, ghq_total, recs_json, req_number
    ))
    
    conn.commit()
    conn.close()
    return req_number