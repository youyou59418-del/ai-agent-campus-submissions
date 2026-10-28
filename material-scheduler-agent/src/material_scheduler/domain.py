"""Validate trusted user input. Tool arguments cannot grant user approval."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json


def canonical_slot(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() != timedelta(hours=8) or dt.minute or dt.second or dt.microsecond:
        raise ValueError("slot must be an hourly ISO timestamp with +08:00 timezone")
    if not 8 <= dt.hour <= 22:
        raise ValueError("business slots are 08:00..22:00 Asia/Shanghai")
    return dt.isoformat()


@dataclass(frozen=True)
class Item:
    material_id: str
    requested_slot: str
    hard_requirement: bool = True
    channel: str = "xiaoying"
    approved_slot: str | None = None

    def fingerprint(self) -> str:
        # Approval can upgrade the same draft. Requirements cannot silently change.
        value = asdict(self)
        value.pop("approved_slot")
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Request:
    request_id: str
    items: tuple[Item, ...]

    @classmethod
    def parse(cls, value: dict) -> "Request":
        if set(value) != {"request_id", "items"}:
            raise ValueError("request requires exactly request_id and items")
        if not isinstance(value["request_id"], str) or not value["request_id"].strip():
            raise ValueError("request_id must be nonempty")
        if not isinstance(value["items"], list) or not 1 <= len(value["items"]) <= 10:
            raise ValueError("items must contain 1..10 materials")
        items = []
        for raw in value["items"]:
            item = Item(**raw)
            if not isinstance(item.material_id, str) or not item.material_id.strip():
                raise ValueError("invalid material_id")
            if not isinstance(item.channel, str) or not item.channel.strip():
                raise ValueError("invalid channel")
            if type(item.hard_requirement) is not bool:
                raise ValueError("hard_requirement must be boolean")
            item = replace(item, requested_slot=canonical_slot(item.requested_slot),
                           approved_slot=canonical_slot(item.approved_slot) if item.approved_slot is not None else None)
            items.append(item)
        if len({item.material_id for item in items}) != len(items):
            raise ValueError("duplicate material IDs within request")
        return cls(value["request_id"], tuple(items))

    def as_dict(self) -> dict:
        return {"request_id": self.request_id, "items": [asdict(i) for i in self.items]}


def candidate_slots(item: Item) -> list[str]:
    start = datetime.fromisoformat(item.requested_slot)
    candidates = [start + timedelta(hours=i) for i in range(4)]
    result = [x.isoformat() for x in candidates if x.date() == start.date() and x.hour <= 22]
    if item.approved_slot and item.approved_slot not in result:
        result.append(item.approved_slot)
    return result
