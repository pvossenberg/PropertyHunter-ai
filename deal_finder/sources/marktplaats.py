from __future__ import annotations

from typing import Any

from deal_finder.sources.base import EmptySourceAdapter


class MarktplaatsAdapter(EmptySourceAdapter):
    source_name = "marktplaats.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.marktplaats.nl/"

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        return super().validate_configuration(configuration)
