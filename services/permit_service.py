from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from models.permit import PermitRecord


@dataclass(frozen=True)
class PermitResponseMeta:
    """Metadata that clarifies source confidence and mock-data status."""

    address: str
    is_mock_data: bool
    confidence: str
    provider: str

    def to_dict(self) -> dict[str, str | bool]:
        """Return metadata as a serializable dictionary."""
        return {
            "address": self.address,
            "is_mock_data": self.is_mock_data,
            "confidence": self.confidence,
            "provider": self.provider,
        }


class PermitDataProvider(Protocol):
    """Provider contract for permit retrieval.

    Implementations can later connect to municipal permit registers and
    commercial datasets while preserving the service's public interface.
    """

    def get_permits(self, normalized_address: str) -> list[PermitRecord]:
        """Return permit records for a normalized address."""


class MockPermitDataProvider:
    """Mock permit provider for development and integration testing.

    The output is realistic in shape but must never be treated as verified facts.
    """

    def __init__(self) -> None:
        self._records: dict[str, list[PermitRecord]] = {
            "keizersgracht 123 amsterdam": [
                PermitRecord(
                    application_date=date(2024, 2, 11),
                    decision_date=None,
                    permit_type="omgevingsvergunning",
                    description="Interne verbouwing en functiewijziging begane grond",
                    status="pending",
                    reference_number="MOCK-AMS-2024-001",
                    authority="Gemeente Amsterdam",
                    source="Mock municipal permit feed",
                    source_url=None,
                    confidence="low",
                    affects_investment_case=True,
                    investment_relevance="Lopende aanvraag kan timing en exploitatie beinvloeden.",
                    notes="Mock data only; verify in official municipality portal.",
                ),
                PermitRecord(
                    application_date=date(2022, 5, 3),
                    decision_date=date(2022, 8, 19),
                    permit_type="splitsingsvergunning",
                    description="Splitsing in twee woonunits",
                    status="granted",
                    reference_number="MOCK-AMS-2022-014",
                    authority="Gemeente Amsterdam",
                    source="Mock municipal permit feed",
                    source_url=None,
                    confidence="medium",
                    affects_investment_case=True,
                    investment_relevance="Mogelijke waardetoevoeging door legale splitsing.",
                    notes="Mock data only; verify legal validity and transferability.",
                ),
                PermitRecord(
                    application_date=date(2021, 1, 10),
                    decision_date=date(2021, 4, 27),
                    permit_type="dakopbouw",
                    description="Aanvraag voor optoppen met extra bouwlaag",
                    status="rejected",
                    reference_number="MOCK-AMS-2021-002",
                    authority="Gemeente Amsterdam",
                    source="Mock municipal permit feed",
                    source_url=None,
                    confidence="low",
                    affects_investment_case=True,
                    investment_relevance="Afwijzing is risicosignaal voor transformatiecase.",
                    notes="Mock data only; reason for rejection must be verified.",
                ),
                PermitRecord(
                    application_date=date(2019, 7, 2),
                    decision_date=date(2019, 8, 15),
                    permit_type="gevelwijziging",
                    description="Wijziging voorpui commerciele ruimte",
                    status="withdrawn",
                    reference_number="MOCK-AMS-2019-091",
                    authority="Gemeente Amsterdam",
                    source="Mock municipal permit feed",
                    source_url=None,
                    confidence="low",
                    affects_investment_case=False,
                    investment_relevance="Niet uitgevoerd door intrekking.",
                    notes="Mock data only; check if new aanvraag later is ingediend.",
                ),
            ],
            "coolsingel 50 rotterdam": [],
        }

    def get_permits(self, normalized_address: str) -> list[PermitRecord]:
        """Return mock permit records for known addresses."""
        return list(self._records.get(normalized_address, []))


class PermitService:
    """Service layer for permit history retrieval independent of UI frameworks.

    The public methods return structured dictionaries containing lists of PermitRecord
    objects and metadata fields that remain stable when real providers are added.
    """

    def __init__(self, provider: PermitDataProvider | None = None) -> None:
        self._provider = provider or MockPermitDataProvider()

    def get_active_permits(self, address: str) -> dict[str, object]:
        """Return active permit applications (status=pending) for an address."""
        return self._build_filtered_response(address, allowed_statuses={"pending"})

    def get_permits_last_10_years(self, address: str) -> dict[str, object]:
        """Return permit records with application date in the last 10 years."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        records = self._provider.get_permits(normalized)
        reference = date.today().replace(year=max(1, date.today().year - 10))
        filtered = [record for record in records if record.application_date is not None and record.application_date >= reference]

        return self._response(
            address=address,
            permits=filtered,
            warnings=[] if filtered else ["No permit records found in the last 10 years for this address."],
        )

    def get_granted_permits(self, address: str) -> dict[str, object]:
        """Return permits with granted status for an address."""
        return self._build_filtered_response(address, allowed_statuses={"granted"})

    def get_rejected_permits(self, address: str) -> dict[str, object]:
        """Return permits with rejected status for an address."""
        return self._build_filtered_response(address, allowed_statuses={"rejected"})

    def get_withdrawn_permits(self, address: str) -> dict[str, object]:
        """Return permits with withdrawn status for an address."""
        return self._build_filtered_response(address, allowed_statuses={"withdrawn"})

    def get_permit_summary(self, address: str) -> dict[str, object]:
        """Return summary counts per permit status plus key risk/value signals."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        records = self._provider.get_permits(normalized)
        status_counts = {
            "pending": 0,
            "granted": 0,
            "rejected": 0,
            "withdrawn": 0,
            "other": 0,
        }

        for record in records:
            if record.status in status_counts:
                status_counts[record.status] += 1
            else:
                status_counts["other"] += 1

        warnings: list[str] = []
        if not records:
            warnings.append("No permit records found for this address in current provider data.")

        if status_counts["pending"] > 0:
            warnings.append("Pending permits indicate uncertainty until official decision is published.")
        if status_counts["rejected"] > 0:
            warnings.append("Rejected permits are an important risk signal but not automatically deal-breaking.")

        return {
            **self._meta(address).to_dict(),
            "warnings": self._mock_warning_prefix() + warnings,
            "data": {
                "total_records": len(records),
                "status_counts": status_counts,
                "affecting_investment_case": sum(1 for record in records if record.affects_investment_case),
            },
        }

    def _build_filtered_response(self, address: str, allowed_statuses: set[str]) -> dict[str, object]:
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        records = self._provider.get_permits(normalized)
        filtered = [record for record in records if record.status in allowed_statuses]

        if records and not filtered:
            message = f"No permits with status in {sorted(allowed_statuses)} found for this address."
        elif not records:
            message = "No permit records found for this address in current provider data."
        else:
            message = ""

        warnings = [message] if message else []
        return self._response(address=address, permits=filtered, warnings=warnings)

    def _response(self, address: str, permits: list[PermitRecord], warnings: list[str]) -> dict[str, object]:
        return {
            **self._meta(address).to_dict(),
            "warnings": self._mock_warning_prefix() + warnings,
            "data": {
                "count": len(permits),
                "permits": permits,
            },
        }

    def _meta(self, address: str) -> PermitResponseMeta:
        return PermitResponseMeta(
            address=address,
            is_mock_data=True,
            confidence="low_to_medium",
            provider=self._provider.__class__.__name__,
        )

    def _invalid_address_response(self, address: str) -> dict[str, object]:
        return {
            **self._meta(address).to_dict(),
            "warnings": self._mock_warning_prefix() + ["Invalid or empty address input."],
            "data": {
                "count": 0,
                "permits": [],
            },
        }

    def _mock_warning_prefix(self) -> list[str]:
        return ["Mock data only. Do not treat these outputs as verified municipal facts."]

    def _normalize_address(self, address: str) -> str | None:
        if not isinstance(address, str) or not address.strip():
            return None
        return " ".join(address.lower().split())
