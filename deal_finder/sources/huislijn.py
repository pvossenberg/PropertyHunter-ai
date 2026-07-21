from __future__ import annotations

from deal_finder.sources.base import EmptySourceAdapter


class HuislijnAdapter(EmptySourceAdapter):
    source_name = "huislijn.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.huislijn.nl/"
