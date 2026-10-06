from bs4 import BeautifulSoup
import json
from config import MAPPING_JSON, BASE_DIR

# Read the HTML file
with open(BASE_DIR / "teamlinks.html", "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "html.parser")

mapping = {}

# Find all consultant items
for item in soup.select(".team-item"):
    # Get the link from the image section
    link_tag = item.select_one(".image a")
    if not link_tag:
        continue
    href = link_tag.get("href")
    if not href or not href.startswith("https://nikravan.org/team/"):
        continue

    # Get the full name from the name section
    name_tag = item.select_one(".name h4")
    if not name_tag:
        continue
    full_name = name_tag.get_text(strip=True)

    # Store it
    mapping[full_name] = href

# Save to the JSON file
with open(str(MAPPING_JSON), "w", encoding="utf-8") as f:
    json.dump(mapping, f, ensure_ascii=False, indent=2)

print(f"✅ {len(mapping)} لینک صحیح استخراج و در mapping.json ذخیره شد.")
print("\n📋 نمونه:")
for i, (name, link) in enumerate(list(mapping.items())[:5], 1):
    print(f"  {i}. {name} -> {link}")