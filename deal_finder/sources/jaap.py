from __future__ import annotations

from typing import Any

from deal_finder.sources.base import EmptySourceAdapter


class JaapAdapter(EmptySourceAdapter):
    source_name = "jaap.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.jaap.nl/"

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        return super().validate_configuration(configuration)
