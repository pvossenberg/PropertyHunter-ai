from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from deal_finder.models import NormalizedListing


class ListingSourceAdapter(ABC):
    source_name: str = "unknown"

    @abstractmethod
    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        pass

    @abstractmethod
    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        pass

    @abstractmethod
    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        pass
