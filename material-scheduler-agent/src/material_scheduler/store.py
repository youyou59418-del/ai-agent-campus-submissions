"""SQLite serializes conflict checking + draft writes in one transaction."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import uuid

from .domain import Item, Request


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS drafts (
                request_id TEXT, material_id TEXT, fingerprint TEXT NOT NULL,
                channel TEXT NOT NULL, slot TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(request_id, material_id))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def overlaps(a: str, b: str) -> bool:
        x, y = datetime.fromisoformat(a), datetime.fromisoformat(b)
        return x < y + timedelta(hours=1) and y < x + timedelta(hours=1)

    def _conflicts(self, db, request: Request, item: Item, slot: str, occupied: list) -> list:
        conflicts = [r["reference"] for r in occupied
                     if r["channel"] == item.channel and self.overlaps(r["slot"], slot)]
        for row in db.execute("SELECT * FROM drafts WHERE channel=?", (item.channel,)):
            if (row["request_id"], row["material_id"]) == (request.request_id, item.material_id):
                continue
            if self.overlaps(row["slot"], slot):
                conflicts.append("draft:" + json.loads(row["payload"])["draft_id"])
        return conflicts

    def conflicts(self, request, item, slot, occupied):
        with self.connect() as db:
            return self._conflicts(db, request, item, slot, occupied)

    def get(self, request_id: str, material_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM drafts WHERE request_id=? AND material_id=?",
                             (request_id, material_id)).fetchone()
            return json.loads(row[0]) if row else None

    def count(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM drafts").fetchone()[0]

    def save(self, request: Request, item: Item, slot: str, occupied: list) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM drafts WHERE request_id=? AND material_id=?",
                             (request.request_id, item.material_id)).fetchone()
            existing = json.loads(row["payload"]) if row else None
            if row and row["fingerprint"] != item.fingerprint():
                return {"ok": False, "code": "IDEMPOTENCY_CONFLICT",
                        "message": "same request/material key has different requirements; use a new request_id"}
            if existing and existing["suggested_slot"] != slot:
                old_conflict = self._conflicts(db, request, item, existing["suggested_slot"], occupied)
                if not old_conflict:
                    # Returning the canonical draft prevents drift on a repeated request.
                    return {"ok": True, "code": "EXISTING_DRAFT", "draft": existing}
            conflicts = self._conflicts(db, request, item, slot, occupied)
            if conflicts:
                return {"ok": False, "code": "SLOT_CONFLICT", "conflicts": conflicts}
            prior_approval = bool(existing and existing["suggested_slot"] == slot and existing["approval_source"] == "user_request")
            confirmed = (slot == item.requested_slot or not item.hard_requirement or slot == item.approved_slot or prior_approval)
            draft = {
                "draft_id": existing["draft_id"] if existing else str(uuid.uuid4()),
                "request_id": request.request_id, "material_id": item.material_id,
                "channel": item.channel, "requested_slot": item.requested_slot,
                "suggested_slot": slot, "hard_requirement": item.hard_requirement,
                "requires_confirmation": not confirmed,
                "status": "READY_DRAFT" if confirmed else "PENDING_CONFIRMATION",
                "approval_source": "user_request" if slot == item.approved_slot or prior_approval else None,
                "published": False,
                "revision": 1,
            }
            content_changed = existing and any(existing.get(k) != v for k, v in draft.items() if k != "revision")
            draft["revision"] = (existing["revision"] + bool(content_changed)) if existing else 1
            db.execute("INSERT OR REPLACE INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                       (request.request_id, item.material_id, item.fingerprint(), item.channel,
                        slot, json.dumps(draft, ensure_ascii=False)))
            verified = json.loads(db.execute("SELECT payload FROM drafts WHERE request_id=? AND material_id=?",
                                  (request.request_id, item.material_id)).fetchone()[0])
            if verified != draft:
                raise sqlite3.DatabaseError("read-back mismatch")
            return {"ok": True, "code": "UPDATED" if content_changed else "DUPLICATE" if existing else "SAVED",
                    "draft": verified}
