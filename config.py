import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONOBANK_TOKEN = os.getenv("MONOBANK_TOKEN", "")
MONOBANK_JAR_NAME = os.getenv("MONOBANK_JAR_NAME", "test")
MONOBANK_INTERVAL = int(os.getenv("MONOBANK_INTERVAL", "60"))
EXPORT_DIR = Path(os.getenv("SESSION_EXPORT_DIR", "session_exports"))
STATS_CSV_PATH = Path(os.getenv("STATS_CSV_PATH", "session_stats.csv"))
STATE_FILE = Path(os.getenv("SESSION_STATE_FILE", "session_state.json"))
AUTO_APPROVE_DELAY = int(os.getenv("AUTO_APPROVE_DELAY", "120"))


MONOBANK_API_URL = "https://api.monobank.ua"

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
STATS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
