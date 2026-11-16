import os
import re
import json

# Read the HTML file
with open("team.html", "r", encoding="utf-8") as f:
    html_content = f.read()

# Pattern for individual profile page links (as observed in the HTML)
_base = os.getenv("CENTER_BASE_URL", "https://example.com").rstrip("/")
pattern = r'<h4><a href="(' + re.escape(_base) + r'/team/[^"]+\.html)">'

links = re.findall(pattern, html_content)
# Remove duplicates (if any)
unique_links = list(set(links))

print(f"تعداد لینک‌های یافت شده: {len(unique_links)}")

# Save to the JSON file
with open("consultant_links.json", "w", encoding="utf-8") as f:
    json.dump(unique_links, f, ensure_ascii=False, indent=2)

print("لینک‌ها در فایل consultant_links.json ذخیره شدند.")