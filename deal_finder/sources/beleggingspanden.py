from __future__ import annotations

from deal_finder.sources.base import EmptySourceAdapter


class BeleggingspandenAdapter(EmptySourceAdapter):
    source_name = "beleggingspanden.nl"
    source_type = "broker"
    is_enabled = True
    default_start_url = "https://www.beleggingspanden.nl/nl"
