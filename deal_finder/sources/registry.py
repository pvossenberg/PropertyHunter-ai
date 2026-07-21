from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from deal_finder.sources.base import ListingSourceAdapter
from deal_finder.sources.beleggingspanden import BeleggingspandenAdapter
from deal_finder.sources.funda import FundaAdapter
from deal_finder.sources.funda_business import FundaBusinessAdapter
from deal_finder.sources.generic_feed import GenericFeedAdapter
from deal_finder.sources.huislijn import HuislijnAdapter
from deal_finder.sources.jaap import JaapAdapter
from deal_finder.sources.klusvastgoed import KlusvastgoedAdapter
from deal_finder.sources.marktplaats import MarktplaatsAdapter
from deal_finder.sources.pararius import ParariusAdapter


@dataclass
class SourceRegistryEntry:
    key: str
    adapter: ListingSourceAdapter


class SourceAdapterRegistry:
    def __init__(self, adapters: Iterable[ListingSourceAdapter] | None = None):
        self._entries: dict[str, ListingSourceAdapter] = {}
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: ListingSourceAdapter) -> None:
        for key in _adapter_keys(adapter):
            self._entries[key] = adapter

    def resolve(self, source_name: str) -> ListingSourceAdapter | None:
        normalized = _normalize_key(source_name)
        if not normalized:
            return None
        return self._entries.get(normalized)

    def list_entries(self) -> list[SourceRegistryEntry]:
        seen_ids: set[int] = set()
        rows: list[SourceRegistryEntry] = []
        for key, adapter in sorted(self._entries.items(), key=lambda item: item[0]):
            marker = id(adapter)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            rows.append(SourceRegistryEntry(key=key, adapter=adapter))
        return rows


def build_default_source_registry() -> SourceAdapterRegistry:
    return SourceAdapterRegistry(
        adapters=[
            FundaAdapter(),
            FundaBusinessAdapter(),
            JaapAdapter(),
            HuislijnAdapter(),
            ParariusAdapter(),
            BeleggingspandenAdapter(),
            MarktplaatsAdapter(),
            KlusvastgoedAdapter(),
            GenericFeedAdapter(),
        ]
    )


def _adapter_keys(adapter: ListingSourceAdapter) -> set[str]:
    source_info = adapter.get_source_info()
    base_name = source_info.source_name
    base_name_normalized = _normalize_key(base_name)
    keys = {
        base_name_normalized,
        _normalize_key(base_name.replace(".nl", "")),
        _normalize_key(base_name.replace(".", " ")),
        _normalize_key(base_name.replace(".", "").replace("nl", "")),
    }

    if base_name_normalized == "funda nl":
        keys.update({"funda", "funda nl"})
    if base_name_normalized == "fundainbusinessnl":
        keys.update({"funda in business", "funda business", "fundainbusiness"})
    if base_name_normalized == "beleggingspanden nl":
        keys.update({"beleggingspanden", "beleggings panden"})
    if base_name_normalized == "huislijn nl":
        keys.update({"huislijn"})
    if base_name_normalized == "pararius nl":
        keys.update({"pararius"})
    if base_name_normalized == "klusvastgoed nl":
        keys.update({"klusvastgoed", "klus vastgoed"})

    return {key for key in keys if key}


def _normalize_key(value: str) -> str:
    if not isinstance(value, str):
        return ""
    lowered = " ".join(value.strip().lower().split())
    return "".join(ch for ch in lowered if ch.isalnum() or ch.isspace()).strip()
