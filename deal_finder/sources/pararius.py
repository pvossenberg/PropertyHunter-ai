from __future__ import annotations

from deal_finder.sources.base import EmptySourceAdapter


class ParariusAdapter(EmptySourceAdapter):
    source_name = "pararius.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.pararius.nl/"
