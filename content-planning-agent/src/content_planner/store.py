from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from .domain import TZ


class RequirementConflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    input_json TEXT NOT NULL, started_at TEXT NOT NULL,
                    strategy TEXT, version INTEGER NOT NULL DEFAULT 0, estimate_json TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
                    step_key TEXT NOT NULL, status TEXT NOT NULL, charged_cents INTEGER NOT NULL,
                    minutes INTEGER NOT NULL, result_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempt_request ON attempts(request_id, id);
                CREATE TABLE IF NOT EXISTS plans (
                    request_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS faults (
                    request_id TEXT, fault TEXT, uses INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(request_id, fault));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def ensure_request(self, request):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint FROM requests WHERE request_id=?", (request.request_id,)).fetchone()
            if row and row[0] != request.fingerprint():
                raise RequirementConflict("same request_id with different requirements; use a new ID")
            if not row:
                start = request.start_time or datetime.now(TZ).isoformat()
                db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, NULL, 0, NULL)",
                           (request.request_id, request.fingerprint(), json.dumps(request.as_dict(), ensure_ascii=False), start))

    def read(self, request_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.read(request_id, connection)
        request = dict(db.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone())
        attempts = []
        for row in db.execute("SELECT * FROM attempts WHERE request_id=? ORDER BY id", (request_id,)):
            value = dict(row)
            value["result"] = json.loads(value.pop("result_json"))
            attempts.append(value)
        latest = {}
        for attempt in attempts:
            latest[attempt["step_key"]] = attempt
        row = db.execute("SELECT payload FROM plans WHERE request_id=?", (request_id,)).fetchone()
        return {"request": request, "attempts": attempts, "latest": latest,
                "saved_plan": json.loads(row[0]) if row else None,
                "spent_cents": sum(a["charged_cents"] for a in attempts),
                "elapsed_minutes": sum(a["minutes"] for a in attempts)}

    @staticmethod
    def add_attempt(db, request_id, key, status, cost, minutes, result):
        db.execute("INSERT INTO attempts(request_id,step_key,status,charged_cents,minutes,result_json) VALUES (?,?,?,?,?,?)",
                   (request_id, key, status, cost, minutes, json.dumps(result, ensure_ascii=False)))
        db.execute("UPDATE requests SET version=version+1 WHERE request_id=?", (request_id,))

    @staticmethod
    def fault_once(db, request_id, fault):
        db.execute("INSERT OR IGNORE INTO faults VALUES (?, ?, 0)", (request_id, fault))
        uses = db.execute("SELECT uses FROM faults WHERE request_id=? AND fault=?", (request_id, fault)).fetchone()[0]
        if uses:
            return False
        db.execute("UPDATE faults SET uses=uses+1 WHERE request_id=? AND fault=?", (request_id, fault))
        return True
