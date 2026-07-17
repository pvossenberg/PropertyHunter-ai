import os
from pathlib import Path
import re
import unicodedata

from dotenv import load_dotenv
import requests

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

FUNDA_DEFAULT_SCAN_CITIES = ("Breda", "Amsterdam")
FUNDA_PDOK_GEMEENTE_WFS_URL = (
    "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=bestuurlijkegebieden:Gemeentegebied"
    "&propertyName=naam&outputFormat=application/json"
)


def _funda_sort_key(value: str) -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_value).strip()


def fetch_dutch_municipalities(timeout_seconds: float | None = None) -> list[str]:
    configured_timeout = timeout_seconds
    if configured_timeout is None:
        configured_timeout = float(os.getenv("FUNDA_PLACE_FETCH_TIMEOUT_SECONDS", "8") or 8)

    response = requests.get(FUNDA_PDOK_GEMEENTE_WFS_URL, timeout=max(1.0, float(configured_timeout)))
    response.raise_for_status()
    payload = response.json()

    features = payload.get("features") if isinstance(payload, dict) else []
    if not isinstance(features, list):
        return []

    seen: set[str] = set()
    names: list[str] = []
    for item in features:
        properties = item.get("properties") if isinstance(item, dict) else {}
        name = str((properties or {}).get("naam") or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)

    return sorted(names, key=_funda_sort_key)
