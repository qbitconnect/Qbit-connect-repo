"""MockMapsProvider — deterministic fixture provider (brief §47).

EXPLICITLY a test/development double (brief §55: fakes are allowed ONLY in
marked test/mock providers). Never selectable in production
(config.validate_runtime refuses QBIT_MAPS_PROVIDER=mock).
"""

from __future__ import annotations

_FIXTURES: list[dict] = [
    {
        "business_name": "Spice Garden Restaurant",
        "category": "Restaurant",
        "phone": "+91 11 4000 1000",
        "website": "https://spicegarden.example.com",
        "email": "hello@spicegarden.example.com",
        "address": "12 Connaught Place",
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "rating": 4.4,
        "review_count": 1250,
        "source_url": "https://maps.example.com/place/spice-garden",
    },
    {
        "business_name": "Delhi Bicycle Co.",
        "category": "Sporting Goods",
        "phone": "+91 11 4000 2000",
        "website": "https://delhibicycle.example.com",
        "address": "88 Hauz Khas Village",
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "rating": 4.1,
        "review_count": 340,
        "source_url": "https://maps.example.com/place/delhi-bicycle",
    },
    {
        "business_name": "Karol Bagh Auto Repairs",
        "category": "Auto Repair",
        "phone": "+91 11 4000 3000",
        "address": "5 Old Rohtak Road",
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "rating": 3.9,
        "review_count": 96,
        "source_url": "https://maps.example.com/place/karol-bagh-auto",
    },
    {
        "business_name": "Chandni Chowk Electronics",
        "category": "Electronics",
        "phone": "+91 11 4000 4000",
        "website": "https://ccelectronics.example.com",
        "address": "220 Bhagirath Palace",
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "rating": 4.0,
        "review_count": 512,
        "source_url": "https://maps.example.com/place/cc-electronics",
    },
    {
        "business_name": "Hauz Khas Social",
        "category": "Cafe",
        "phone": "+91 11 4000 5000",
        "website": "https://hksocial.example.com",
        "address": "9A Hauz Khas Village",
        "city": "New Delhi",
        "state": "Delhi",
        "country": "India",
        "rating": 4.5,
        "review_count": 2100,
        "source_url": "https://maps.example.com/place/hk-social",
    },
]


class MockMapsProvider:
    """Deterministic paged fixtures (page_size=2) so checkpoint paths run."""

    name = "mock"

    async def search(
        self,
        *,
        query: str,
        city: str | None,
        state: str | None,
        country: str | None,
        language: str | None,
        page_token: str | None,
        max_results: int,
        http,  # noqa: ARG002 — mock never touches the network
    ) -> tuple[list[dict], str | None]:
        page_size = 2  # fixed page size; `max_results` caps the JOB, not the page
        start = int(page_token or 0)
        end = min(start + page_size, len(_FIXTURES))
        items = [dict(item) for item in _FIXTURES[start:end]]
        next_token = str(end) if end < len(_FIXTURES) else None
        return items, next_token
