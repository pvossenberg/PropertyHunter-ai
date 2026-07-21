from __future__ import annotations

from deal_finder.sources.funda import FundaAdapter


class FundaBusinessAdapter(FundaAdapter):
    source_name = "fundainbusiness.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.fundainbusiness.nl/zoeken/koop"
