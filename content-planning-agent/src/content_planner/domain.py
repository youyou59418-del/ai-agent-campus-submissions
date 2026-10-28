from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

TZ = timezone(timedelta(hours=8))


def cents(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("budget must be a nonnegative number with at most two decimal places")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
            raise ValueError("invalid money amount")
        return int(number * 100)
    except InvalidOperation:
        raise ValueError("invalid money amount") from None


def units(value: int) -> str:
    return format(Decimal(value) / 100, ".2f")


def timestamp(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() != timedelta(hours=8):
        raise ValueError("timestamps must include +08:00 timezone")
    return dt.isoformat()


@dataclass(frozen=True)
class Request:
    request_id: str
    topic: str
    budget_cents: int | None
    deadline: str | None
    start_time: str | None = None
    initial_strategy: str = "premium"

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw) - {"request_id", "topic", "budget", "deadline", "start_time", "initial_strategy"}:
            raise ValueError("invalid request fields")
        rid, topic = raw.get("request_id"), raw.get("topic")
        if not isinstance(rid, str) or not rid.strip() or len(rid) > 120:
            raise ValueError("request_id required")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 500:
            raise ValueError("topic required (1..500 characters)")
        strategy = raw.get("initial_strategy", "premium")
        if strategy not in {"premium", "economy"}:
            raise ValueError("unknown initial strategy")
        return cls(rid, topic, cents(raw["budget"]) if raw.get("budget") is not None else None,
                   timestamp(raw["deadline"]) if raw.get("deadline") else None,
                   timestamp(raw["start_time"]) if raw.get("start_time") else None, strategy)

    @property
    def missing(self):
        return [field for field, value in (("budget", self.budget_cents), ("deadline", self.deadline)) if value is None]

    def as_dict(self):
        value = asdict(self)
        value["budget"] = units(value.pop("budget_cents")) if self.budget_cents is not None else None
        return value

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
