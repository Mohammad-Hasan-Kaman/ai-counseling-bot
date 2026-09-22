# -*- coding: utf-8 -*-
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
PROFILES_JSON = BASE_DIR / "consultants_profile_full.json"
MAPPING_JSON = BASE_DIR / "mapping.json"
APPOINTMENTS_DB = BASE_DIR / "appointments.db"
USER_RECORDS_DB = BASE_DIR / "user_records.db"
CRAWLER_LOCK = BASE_DIR / "crawler.lock"

# Bale bot token — environment only (.env on the server); never commit it to git
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Crawler schedule (every 2 hours)
CRAWLER_INTERVAL_HOURS = 2
CRAWLER_DELAY_SECONDS = 1
CRAWLER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Admin user IDs — environment only; comma-separated, e.g. "123456,789012"
ADMIN_USER_IDS: set = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

# Admin panel password — environment only; never commit it to git
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")