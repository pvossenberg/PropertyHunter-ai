from __future__ import annotations

from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


def fetch_page_text(url: str) -> str:
    if not url or not url.strip():
        raise ValueError("Geef een URL op.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Gebruik een geldige http(s)-URL.")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PropertyHunterAI/1.0; +https://example.com/propertyhunter)"
    }

    try:
        response = requests.get(url.strip(), headers=headers, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise requests.RequestException(f"De pagina kon niet worden opgehaald: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    text = soup.get_text(separator=" ", strip=True)
    text = " ".join(text.split())
    return text[:15000]
