import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def get_openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip()


OPENAI_API_KEY = get_openai_api_key()
