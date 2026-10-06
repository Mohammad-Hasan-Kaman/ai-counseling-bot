# -*- coding: utf-8 -*-
import json
import logging
import sqlite3
import time
import requests
from bs4 import BeautifulSoup
from config import APPOINTMENTS_DB, MAPPING_JSON, CRAWLER_DELAY_SECONDS, CRAWLER_HEADERS

log = logging.getLogger(__name__)
TEAM_URL = "https://nikravan.org/team/"

# The system proxy (e.g. a VPN client on port 10808) is sometimes unstable.
# Strategy: try the system proxy first; on failure, connect directly (no proxy)
DIRECT_PROXIES = {"http": None, "https": None}   # bypass the system proxy
FETCH_RETRIES = 2          # each URL is tried at most twice
RETRY_BACKOFF = 3          # seconds to wait between retries


def fetch_page(url: str) -> str:
    """
    Fetch a website page with resilience:
    Attempt 1: default route (system proxy if enabled)
    Attempt 2: after backoff, direct connection without a proxy
    """
    last_err = None
    for attempt in range(FETCH_RETRIES):
        proxies = DIRECT_PROXIES if attempt > 0 else None
        try:
            resp = requests.get(url, headers=CRAWLER_HEADERS, timeout=15, proxies=proxies)
            return resp.text
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES - 1:
                log.warning("⚠️ تلاش %d برای %s ناموفق بود؛ %d ثانیه دیگر با مسیر جایگزین...", attempt + 1, url.split('/')[-1], RETRY_BACKOFF)
                time.sleep(RETRY_BACKOFF)
    raise last_err


def _db(sql, params=(), fetch=False):
    conn = sqlite3.connect(str(APPOINTMENTS_DB))
    cur = conn.cursor()
    cur.execute(sql, params)
    result = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return result


def init_db():
    _db("""CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        counselor_name TEXT, date TEXT, time TEXT, room TEXT,
        status TEXT, branch TEXT, fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")


def scrape_team_list() -> int:
    try:
        html = fetch_page(TEAM_URL)
        soup = BeautifulSoup(html, "html.parser")

        mapping = {}
        for heading in soup.select("h4 a"):
            href = heading.get("href", "")
            name = heading.get_text(strip=True)
            if name and href and "team/" in href:
                if not href.startswith("http"):
                    href = "https://nikravan.org" + href
                mapping[name] = href

        if mapping:
            with open(MAPPING_JSON, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            log.info("📋 %d مشاور از سایت دریافت و در mapping.json ذخیره شد.", len(mapping))
        return len(mapping)
    except Exception as e:
        log.error("❌ خطای استخراج لیست مشاوران: %s", e)
        return 0


def crawl_available_slots():
    scrape_team_list()
    init_db()
    try:
        with open(MAPPING_JSON, "r", encoding="utf-8") as f:
            mapping = json.load(f)
    except FileNotFoundError:
        log.error("mapping.json یافت نشد.")
        return

    log.info("🔍 در حال استخراج نوبت‌های %d مشاور...", len(mapping))

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5   # stop the whole crawl after 5 consecutive failures (network fully down)

    for name, url in mapping.items():
        try:
            html = fetch_page(url)
        except Exception as e:
            # Network/proxy error: this counselor's previous data is left untouched
            consecutive_errors += 1
            log.error("❌ خطای اتصال هنگام کراول %s: %s", name, e)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("🛑 اتصال به سایت برقرار نیست؛ کراول متوقف شد و داده قبلی حفظ ماند.")
                break
            time.sleep(CRAWLER_DELAY_SECONDS)
            continue

        consecutive_errors = 0

        try:
            soup = BeautifulSoup(html, "html.parser")

            # Priority 1: the appointments table — the phrases "waiting list"/"phone call" may also appear
            # in the menu or footer of any page, so the table has to be checked first.
            table = soup.find("table", {"id": "report"}) or soup.find("table", class_="table")
            free_slots = []
            if table:
                for row in (table.find("tbody") or table).find_all("tr"):
                    if row.get("data-status") != "free":
                        continue
                    cols = row.find_all("td")
                    if len(cols) < 3:
                        continue
                    link = cols[2].find("a", href=True)
                    if link and "schedules_edit" in link["href"]:
                        date = cols[0].get_text(strip=True)
                        span = cols[1].find("span")
                        time_slot = span.get_text(strip=True) if span else ""
                        room_el = cols[1].find("small")
                        room_txt = room_el.get_text(strip=True) if room_el else ""
                        branch = row.get("data-branch", "")
                        # Infer the branch from the room text when data-branch is empty
                        if not branch and room_txt:
                            if "ظفر" in room_txt or "زعفرانیه" in room_txt:
                                branch = "zafar"
                            elif "ایران" in room_txt:
                                branch = "iran"
                        free_slots.append((date, time_slot, room_txt, branch))

            if free_slots:
                _db("DELETE FROM appointments WHERE counselor_name=?", (name,))
                for date, time_slot, room_txt, branch in free_slots:
                    _db(
                        "INSERT INTO appointments (counselor_name,date,time,room,status,branch) VALUES (?,?,?,?,?,?)",
                        (name, date, time_slot, room_txt, "free", branch),
                    )
                log.info("🟢 %s: %d نوبت آزاد ثبت شد.", name, len(free_slots))
                time.sleep(CRAWLER_DELAY_SECONDS)
                continue

            # Priority 2: status messages — valid only when the table has no free slots
            page_text = soup.get_text()
            _db("DELETE FROM appointments WHERE counselor_name=?", (name,))

            if "هم اکنون نوبت آزاد ندارد" in page_text:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "no_available"))
            elif "تماس تلفنی" in page_text or "صرفا با تماس" in page_text:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "phone_only"))
            elif "لیست انتظار" in page_text or table is not None:
                # A table without free rows means everything is booked; even the mere word "waiting list" in the menu counts as waiting
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "waiting"))
            else:
                _db("INSERT INTO appointments (counselor_name,status) VALUES (?,?)", (name, "no_table"))

        except Exception as e:
            log.error("❌ خطای پردازش صفحه %s: %s", name, e)

        time.sleep(CRAWLER_DELAY_SECONDS)

    log.info("✅ فرآیند استخراج نوبت‌ها تکمیل شد.")


if __name__ == "__main__":
    crawl_available_slots()