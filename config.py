# config.py

import os

from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            out.append(int(part))
    return out


class Config:
    API_ID = int(os.environ.get("API_ID", "0") or 0)
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    MONGO_URI = os.environ.get("MONGO_URI", "")
    DB_NAME = os.environ.get("DB_NAME", "cloner_boy")
    ADMINS = _int_list(os.environ.get("ADMINS", ""))
    # Encrypt session strings / bot tokens at rest. Prefer SESSION_ENC_KEY.
    SESSION_ENC_KEY = (
        os.environ.get("SESSION_ENC_KEY", "")
        or os.environ.get("SESSION_SECRET", "")
        or os.environ.get("ENCRYPTION_KEY", "")
    )
    SESSION_SECRET = SESSION_ENC_KEY  # alias
