"""Settings read from the environment once, at startup."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _emails(name: str) -> set[str]:
    return {e.strip().lower() for e in os.environ.get(name, "").split(",") if e.strip()}


MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
APP_NAME = os.environ.get("APP_NAME", "agos")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")

CLERK_PUBLISHABLE_KEY = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
ENV = "live" if CLERK_PUBLISHABLE_KEY.startswith("pk_live_") else "preview"
SUPER_ADMIN_EMAILS = _emails("SUPER_ADMIN_EMAIL")
PLATFORM_ADMIN_EMAILS = _emails("PLATFORM_ADMIN_EMAIL")

STELLAR_HORIZON_URL = os.environ["STELLAR_HORIZON_URL"]
STELLAR_NETWORK = os.environ["STELLAR_NETWORK"]
STELLAR_ENCRYPTION_KEY = os.environ["STELLAR_ENCRYPTION_KEY"]
