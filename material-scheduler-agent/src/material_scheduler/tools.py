"""Business tools trust validated user input and fixtures, never model claims."""

import json
from pathlib import Path
import sqlite3

from .domain import Request, candidate_slots, canonical_slot
from .store import Store


class BusinessTools:
    def __init__(self, request: Request, store: Store, data_dir: Path, *, fail_save=False):
        self.request, self.store, self.data_dir = request, store, Path(data_dir)
        self.items = {item.material_id: item for item in request.items}
        self.materials = json.loads((self.data_dir / "materials.json").read_text(encoding="utf-8"))
        self.supplements = json.loads((self.data_dir / "supplements.json").read_text(encoding="utf-8"))
        self.rules = json.loads((self.data_dir / "rules.json").read_text(encoding="utf-8"))
        self.loaded, self.reviews, self.queries, self.saved, self.errors = {}, {}, {}, {}, {}
        self.no_slots = set()
        self.fail_save = fail_save

    def occupied(self):
        # Re-read at save time; the external fixture may change after query.
        return json.loads((self.data_dir / "schedule.json").read_text(encoding="utf-8"))["occupied"]

    def read_material(self, material_id: str, include_supplement: bool):
        item = self.materials.get(material_id)
        if item is None:
            self.reviews[material_id] = {"review_status": "NEEDS_INFO", "findings": [
                {"rule_id": "SOURCE", "field": "material_id", "severity": "missing", "reason": "素材库未找到该 ID"}]}
            return {"ok": False, "code": "MATERIAL_NOT_FOUND"}
        item = dict(item)
        if include_supplement:
            item.update(self.supplements.get(material_id, {}))
        self.loaded[material_id] = item
        self.reviews.pop(material_id, None)
        return {"ok": True, "material": item, "supplement_checked": include_supplement,
                "supplement_available": material_id in self.supplements}

    def check_rules(self, material_id: str):
        if material_id not in self.loaded:
            return {"ok": False, "code": "READ_REQUIRED"}
        material = self.loaded[material_id]
        findings = []
        for rule in self.rules["rules"]:
            value = material.get(rule["field"])
            missing = value is None or value == "" or (isinstance(value, str) and not value.strip())
            if missing or (rule["kind"] == "must_be_true" and value is not True):
                findings.append({"rule_id": rule["id"], "field": rule["field"],
                                 "severity": "missing" if missing else rule["severity"],
                                 "reason": rule["message"]})
        severities = {f["severity"] for f in findings}
        status = "BLOCKED" if "block" in severities else "NEEDS_INFO" if "missing" in severities else "NEEDS_REVIEW" if findings else "APPROVED"
        result = {"ok": True, "review_status": status, "findings": findings,
                  "rules_version": self.rules["version"]}
        self.reviews[material_id] = result
        return result

    def query_slots(self, material_id: str, include_alternatives: bool):
        item = self.items[material_id]
        slots = candidate_slots(item) if include_alternatives else [item.requested_slot]
        existing = self.store.get(self.request.request_id, material_id)
        if existing and existing["suggested_slot"] not in slots:
            slots.append(existing["suggested_slot"])
        occupied = self.occupied()
        results = []
        for slot in slots:
            conflicts = self.store.conflicts(self.request, item, slot, occupied)
            results.append({"slot": slot, "available": not conflicts, "conflicts": conflicts})
        self.queries.setdefault(material_id, set()).update(slots)
        if include_alternatives and not any(r["available"] for r in results):
            self.no_slots.add(material_id)
        else:
            self.no_slots.discard(material_id)
        result = {"ok": True, "slots": results, "existing_draft": existing,
                  "approval_scope": item.approved_slot,
                  "notice": "availability is rechecked on save; alternatives are proposals, not user approvals"}
        return result

    def save_draft(self, material_id: str, slot: str):
        item = self.items[material_id]
        slot = canonical_slot(slot)
        if self.reviews.get(material_id, {}).get("review_status") != "APPROVED":
            return {"ok": False, "code": "REVIEW_NOT_APPROVED"}
        if slot not in candidate_slots(item):
            return {"ok": False, "code": "OUT_OF_WINDOW"}
        if slot not in self.queries.get(material_id, set()):
            return {"ok": False, "code": "QUERY_REQUIRED"}
        try:
            if self.fail_save:
                raise sqlite3.OperationalError("injected storage outage")
            result = self.store.save(self.request, item, slot, self.occupied())
        except sqlite3.Error:
            result = {"ok": False, "code": "SAVE_FAILED", "message": "draft was not confirmed saved"}
        if result["ok"]:
            self.saved[material_id] = result["draft"]
            self.errors.pop(material_id, None)
        else:
            self.errors[material_id] = result["code"]
            self.saved.pop(material_id, None)
        return result

    def call(self, name: str, args: dict):
        spec = next((t["function"] for t in TOOL_SCHEMAS if t["function"]["name"] == name), None)
        if not spec:
            return {"ok": False, "code": "TOOL_NOT_ALLOWED"}
        if not isinstance(args, dict) or set(args) != set(spec["parameters"]["required"]):
            return {"ok": False, "code": "INVALID_ARGUMENTS"}
        for key, prop in spec["parameters"]["properties"].items():
            expected = str if prop["type"] == "string" else bool
            if type(args[key]) is not expected or (expected is str and not args[key].strip()):
                return {"ok": False, "code": "INVALID_ARGUMENTS"}
        if name == "finish":
            remaining = [mid for mid in self.items
                         if mid not in self.saved and mid not in self.errors
                         and mid not in self.no_slots
                         and self.reviews.get(mid, {}).get("review_status") not in {"NEEDS_INFO", "BLOCKED", "NEEDS_REVIEW"}]
            if remaining:
                return {"ok": False, "code": "FINISH_REJECTED", "remaining_materials": remaining,
                        "message": "complete remaining items using tools; pending-confirmation drafts are authorized and must be saved before finish"}
            return {"ok": True, "code": "FINISHED"}
        args = {k: v for k, v in args.items() if k != "reason"}
        if args["material_id"] not in self.items:
            return {"ok": False, "code": "OUT_OF_SCOPE"}
        try:
            return getattr(self, name)(**args)
        except (ValueError, TypeError):
            return {"ok": False, "code": "INVALID_ARGUMENTS"}
        except (OSError, json.JSONDecodeError, sqlite3.Error):
            return {"ok": False, "code": "TOOL_FAILURE"}

    def report(self, *, termination: str) -> dict:
        results = []
        for material_id, item in self.items.items():
            review = self.reviews.get(material_id, {})
            draft = self.saved.get(material_id)
            error = self.errors.get(material_id)
            stale = bool(draft and self.store.conflicts(self.request, item, draft["suggested_slot"], self.occupied()))
            if stale:
                error, draft = "DRAFT_STALE", None
            review_status = review.get("review_status", "UNKNOWN")
            outcome = "DRAFT_SAVED" if draft else "SAVE_FAILED" if error else review_status if review_status in {"NEEDS_INFO", "BLOCKED", "NEEDS_REVIEW"} else "NO_AVAILABLE_SLOT" if material_id in self.no_slots else "INCOMPLETE"
            pending = (["confirm_alternative_slot"] if draft and draft["requires_confirmation"] else
                       [f["field"] for f in review.get("findings", [])] if review_status in {"NEEDS_INFO", "NEEDS_REVIEW"} else
                       ["provide_new_time_window"] if material_id in self.no_slots else [])
            results.append({"material_id": material_id, "review_status": review_status,
                            "risk_reasons": review.get("findings", []), "requested_slot": item.requested_slot,
                            "suggested_slot": draft["suggested_slot"] if draft else None,
                            "pending_items": pending, "outcome": outcome,
                            "draft_save_status": "SAVED" if draft else "FAILED" if error else "NOT_ATTEMPTED",
                            "draft": draft, "error": error, "published": False})
        complete = termination == "FINISHED" and all(r["outcome"] != "INCOMPLETE" for r in results)
        return {"request_id": self.request.request_id, "status": "COMPLETED" if complete else "INCOMPLETE",
                "termination": termination, "items": results,
                "notice": "Only local drafts. COMPLETED means the review run ended; it does not mean publication or user confirmation."}


def tool_schema(name, description, properties):
    properties = {**properties, "reason": {"type": "string", "description": "One short action rationale; no private chain of thought."}}
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}, "strict": True}}


MID = {"material_id": {"type": "string"}}
TOOL_SCHEMAS = [
    tool_schema("read_material", "Read synthetic material metadata; optionally look up supplementary source.",
                {**MID, "include_supplement": {"type": "boolean"}}),
    tool_schema("check_rules", "Check the last read against authoritative rules; required before saving.", MID),
    tool_schema("query_slots", "Check requested time, or requested time plus next 3 hourly slots and explicit user approval. Includes existing draft.",
                {**MID, "include_alternatives": {"type": "boolean"}}),
    tool_schema("save_draft", "Save only a draft at an observed free slot. The tool derives confirmation, rechecks conflicts and deduplicates.",
                {**MID, "slot": {"type": "string"}}),
    tool_schema("finish", "End after every item has a saved draft or a documented unresolved/risk/failure result.", {}),
]
