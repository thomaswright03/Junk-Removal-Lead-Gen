from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Lead:
    """One property that probably needs a clean-out.

    ``source`` + ``source_id`` identify the record upstream (e.g. a justice
    court case number) and are what make re-runs idempotent.
    """

    source: str
    source_id: str
    lead_type: str  # eviction | code_violation | foreclosure | manual
    event_date: Optional[str] = None  # ISO date the signal happened (filed, hearing, opened)
    address: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    in_pima: Optional[bool] = None
    plaintiff: Optional[str] = None  # landlord / property manager: the likely customer
    defendant: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
