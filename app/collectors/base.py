from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CollectorTarget:
    product_id: str
    url: str


@dataclass(frozen=True, slots=True)
class CollectorResult:
    collected_at: datetime
    collector_name: str
    collector_version: str
    parse_status: str
    cumulative_sold: int | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    review_count: int | None = None
    title: str | None = None
    source_http_status: int | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    raw_content: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class Collector(Protocol):
    name: str
    version: str

    def collect(self, target: CollectorTarget) -> CollectorResult: ...

