import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def get_openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


def get_supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip()


def get_supabase_service_role_key() -> str:
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_KEY", "")).strip()


OPENAI_API_KEY = get_openai_api_key()
SUPABASE_URL = get_supabase_url()
SUPABASE_SERVICE_ROLE_KEY = get_supabase_service_role_key()
