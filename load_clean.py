import os
import json
import re
import pandas as pd
from config import PROFILES_JSON, BASE_DIR

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
    print("🗑️ دیتابیس قبلی حذف شد.")

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE consultants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    gender TEXT,
    specialties TEXT,
    sub_specialties TEXT,
    work_experience_years REAL,
    age_range_min INTEGER,
    age_range_max INTEGER,
    description TEXT,
    embedding TEXT,
    profile_url TEXT
)
''')
conn.commit()
print("✅ جدول consultants ساخته شد.")

EXCEL_PATH = 'data/همکاران نیک روان.xlsx'
df = pd.read_excel(EXCEL_PATH, sheet_name='Results', header=None)

header_row = df.iloc[0]
data_rows = df.iloc[3:]

# ================== لیست مشاوران مورد نظر (یکتا) ==================
TARGET_NAMES = {
    "ریحانه سادات مدنی",
    "محمدعلی نوری",
    "سید امیر زرباف",
    "ریحانه صبورنژاد",
    "طلعت آهنگر",
    "ثمینه خندان",
    "سوده علی بخشیان",
    "راحله گرجی",
    "محمدابراهیم کلباسی",
    "حمید رضا افسری",
    "فهیمه سابقی",
    "فاطمه سعیدیان",
    "محمدعلی رفیق‌دوست",
    "حامد مجدی",
    "زهرا مجاهدی",
    "شادی نوروزعلی",
    "زهرا نیلی",
    "زینب السادات میرجعفری",
    "شاهین رضا ادیبی",
    "مهساامیدبیکی",
    "زهرا جواهری محمدی",
    "محمدباقردربندی",
    "جلایی فر",
    "سارا رضایی",
    "زهرا نعمتی‌پور",
    "محمدصادق رمضانی زاده",
    "سمیه آزاد",
    "شقایق ملکوتی خواه",
    "سپیده خراسانچی",
    "فاطمه مقربان",
    "مونا هاشمی",
    "فاطمه عزیززاده",
    "لیلا محمدزاده",
    "زهراسادات عطاردی",
    "فرخ لقا عکافی",
    "معصومه سادات افضلی",
    "محیا سادات شاه‌صاحبی",
    "رضا غفارزاده نمازی",
    "اسماعیل اسماعیلی شریف",
    "نداطالب",
    "زینب علی پور",
    "زهرا مسگریان",
    "زهرا دانشیان",
    "شیما قبا",
    "زهرا کربلائی",
    "هاله آمیغی"
}
# ================================================================

# پیدا کردن ایندکس ستون‌ها
col_name = None
col_education = None
col_age = None
col_general = []
col_sub = []

for idx, val in header_row.items():
    if pd.notna(val):
        val_str = str(val).strip()
        if 'نام و نام خانوادگی مشاور' in val_str:
            col_name = idx
        elif 'تحصیلات و سوابق کاری' in val_str:
            col_education = idx
        elif 'محدوده سن پذیرش' in val_str:
            col_age = idx
        elif 'حوزه فعالیت عمومی' in val_str or 'حوزه فعالیت ها عمومی' in val_str:
            col_general.append(idx)
        elif 'موضوعات مورد پذیرش بصورت جزئی تر' in val_str:
            col_sub.append(idx)

print(f"📍 ستون نام: {col_name}")
print(f"📍 ستون سوابق: {col_education}")
print(f"📍 ستون محدوده سنی: {col_age}")
print(f"📍 ستون‌های تخصص عمومی: {col_general}")
print(f"📍 ستون‌های تخصص جزئی: {col_sub}")

def extract_years(text):
    if pd.isna(text):
        return 0
    m = re.search(r'(\d+)\s*سال', str(text))
    return int(m.group(1)) if m else 0

def guess_gender(name):
    female = ['مریم', 'ریحانه', 'زهرا', 'فاطمه', 'سوده', 'راحله', 'مونا', 'شقایق', 'زینب', 'سمیه', 'ندا', 'هاله']
    male = ['محمد', 'سید', 'رضا', 'حامد', 'مهدی', 'علی', 'حمید', 'شاهین', 'اسماعیل', 'حمیدرضا']
    for f in female:
        if f in name:
            return 'female'
    for m in male:
        if m in name:
            return 'male'
    return 'unknown'

def parse_age_range(s):
    if pd.isna(s):
        return None, None
    s = str(s)
    m = re.search(r'(\d+)\s*تا\s*(\d+)', s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r'بالای\s*(\d+)', s)
    if m:
        return int(m.group(1)), 120
    m = re.search(r'(\d+)\s*سال به بالا', s)
    if m:
        return int(m.group(1)), 120
    return None, None

def extract_specialties(text):
    if pd.isna(text):
        return []
    text = str(text).replace('\n', ',')
    parts = [p.strip() for p in text.split(',') if p.strip()]
    return parts

# بارگذاری لینک‌ها از mapping
profile_url_map = {}
try:
    with open("consultant_mapping.json", "r", encoding="utf-8") as f:
        temp = json.load(f)
        profile_url_map = {v: k for k, v in temp.items()}
    print(f"📋 {len(profile_url_map)} لینک از mapping بارگذاری شد.")
except:
    print("⚠️ فایل consultant_mapping.json یافت نشد.")

count = 0
found_names = set()

for idx, row in data_rows.iterrows():
    name = row[col_name] if col_name is not None and pd.notna(row[col_name]) else None
    if not name:
        continue

    # حذف فاصله‌های اضافی و عادی‌سازی
    name = name.strip()
    if name not in TARGET_NAMES:
        continue

    if name in found_names:
        print(f"⚠️ نام تکراری: {name} - رد شد.")
        continue
    found_names.add(name)

    edu = row[col_education] if col_education is not None else ''
    years = extract_years(edu)
    gender = guess_gender(name)
    age_range = row[col_age] if col_age is not None else ''
    age_min, age_max = parse_age_range(age_range)
    profile_url = profile_url_map.get(name, None)

    # استخراج تخصص عمومی
    general = []
    for col in col_general:
        if col is not None and pd.notna(row[col]):
            general.extend(extract_specialties(row[col]))
    general = list(set(general))

    # استخراج تخصص جزئی
    sub = {}
    for col in col_sub:
        if col is not None and pd.notna(row[col]):
            for item in extract_specialties(row[col]):
                if item:
                    sub[item] = item

    cursor.execute('''
        INSERT INTO consultants 
        (name, gender, specialties, sub_specialties, work_experience_years, age_range_min, age_range_max, description, embedding, profile_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (name, gender, json.dumps(general, ensure_ascii=False), json.dumps(sub, ensure_ascii=False),
          years, age_min, age_max, f"تخصص‌ها: {', '.join(general)}. {json.dumps(sub, ensure_ascii=False)}", '[]', profile_url))

    count += 1
    if count % 10 == 0:
        print(f"⏳ بارگذاری: {count} مشاور")

conn.commit()
print(f"✅ {count} مشاور با موفقیت در دیتابیس ذخیره شد.")

cursor.execute("SELECT COUNT(*) FROM consultants")
total = cursor.fetchone()[0]
print(f"📊 تعداد کل مشاوران در دیتابیس: {total}")

# بررسی نام‌های پیدا نشده
missing = TARGET_NAMES - found_names
if missing:
    print(f"⚠️ این نام‌ها در فایل اکسل پیدا نشدند: {missing}")

conn.close()
print("🎉 بارگذاری کامل شد.")