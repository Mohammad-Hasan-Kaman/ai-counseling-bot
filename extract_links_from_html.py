import re
import json

# خواندن فایل HTML
with open("team.html", "r", encoding="utf-8") as f:
    html_content = f.read()

# الگوی لینک صفحات اختصاصی (مشاهده شده در HTML)
pattern = r'<h4><a href="(https://nikravan.org/team/[^"]+\.html)">'

links = re.findall(pattern, html_content)
# حذف تکراری‌ها (اگر باشند)
unique_links = list(set(links))

print(f"تعداد لینک‌های یافت شده: {len(unique_links)}")

# ذخیره در فایل JSON
with open("consultant_links.json", "w", encoding="utf-8") as f:
    json.dump(unique_links, f, ensure_ascii=False, indent=2)

print("لینک‌ها در فایل consultant_links.json ذخیره شدند.")