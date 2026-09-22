from bs4 import BeautifulSoup
import json
from config import MAPPING_JSON, BASE_DIR

# فایل HTML رو بخون
with open(BASE_DIR / "teamlinks.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "html.parser")

mapping = {}

# تمام المان‌های مشاور رو پیدا کن
for item in soup.select(".team-item"):
    # لینک رو از بخش image بگیر
    link_tag = item.select_one(".image a")
    if not link_tag:
        continue
    href = link_tag.get("href")
    if not href or not href.startswith("https://nikravan.org/team/"):
        continue

    # اسم کامل رو از بخش name بگیر
    name_tag = item.select_one(".name h4")
    if not name_tag:
        continue
    full_name = name_tag.get_text(strip=True)

    # ذخیره کن
    mapping[full_name] = href

# ذخیره به فایل JSON
with open(str(MAPPING_JSON), "w", encoding="utf-8") as f:
    json.dump(mapping, f, ensure_ascii=False, indent=2)

print(f"✅ {len(mapping)} لینک صحیح استخراج و در mapping.json ذخیره شد.")
print("\n📋 نمونه:")
for i, (name, link) in enumerate(list(mapping.items())[:5], 1):
    print(f"  {i}. {name} -> {link}")