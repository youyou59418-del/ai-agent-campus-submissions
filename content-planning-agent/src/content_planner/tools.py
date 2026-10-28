"""Authoritative dependencies, fixed fees, virtual clock, and atomic checkpoint writes."""
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .domain import cents, units

STRATEGIES = ("premium", "economy")
FAULTS = ("generate_once", "generate_always", "search_partial", "save_once", "save_always")


class BusinessTools:
    def __init__(self, request, store, data_dir, faults=()):
        self.request, self.store = request, store
        self.faults = set(faults)
        if self.faults - set(FAULTS):
            raise ValueError("unknown synthetic fault")
        data_dir = Path(data_dir)
        self.fees = json.loads((data_dir / "costs.json").read_text(encoding="utf-8"))
        self.catalog = json.loads((data_dir / "materials.json").read_text(encoding="utf-8"))
        for category in ("search", "script"):
            for fee in self.fees[category].values():
                cents(fee["cost"])
                if type(fee["minutes"]) is not int or fee["minutes"] < 0:
                    raise ValueError("invalid fee minutes")
        self.last_error = None
        store.ensure_request(request)

    def _materials(self, state):
        result = {}
        for key, step in state["latest"].items():
            if key.startswith("search:") and step["status"] in {"SUCCEEDED", "PARTIAL"}:
                for item in step["result"].get("materials", []):
                    result[item["id"]] = item
        return list(result.values())

    def _quote(self, strategy, state):
        script_fee = self.fees["script"][strategy]
        script = state["latest"].get("script:" + strategy)
        script_done = bool(script and script["status"] == "SUCCEEDED")
        need_search = not script_done and len(self._materials(state)) < script_fee["min_materials"]
        source = "backup" if "search:primary" in state["latest"] else "primary"
        remaining_cost = 0
        remaining_minutes = 0
        if not state["saved_plan"]:
            if need_search:
                remaining_cost += cents(self.fees["search"][source]["cost"])
                remaining_minutes += self.fees["search"][source]["minutes"]
            if not script_done:
                remaining_cost += cents(script_fee["cost"])
                remaining_minutes += script_fee["minutes"]
            remaining_cost += cents(self.fees["save"]["cost"])
            remaining_minutes += self.fees["save"]["minutes"]
        total = state["spent_cents"] + remaining_cost
        finish = datetime.fromisoformat(state["request"]["started_at"]) + timedelta(minutes=state["elapsed_minutes"] + remaining_minutes)
        deadline = datetime.fromisoformat(self.request.deadline)
        return {"strategy": strategy, "spent_units": units(state["spent_cents"]),
                "remaining_units": units(remaining_cost), "estimated_total_units": units(total),
                "remaining_minutes": remaining_minutes, "estimated_finish": finish.isoformat(),
                "budget_gap_units": units(max(0, total - self.request.budget_cents)),
                "deadline_gap_minutes": max(0, math.ceil((finish - deadline).total_seconds() / 60)),
                "feasible": total <= self.request.budget_cents and finish <= deadline,
                "min_materials": script_fee["min_materials"], "needs_more_materials": need_search}

    def state(self):
        state = self.store.read(self.request.request_id)
        strategy = state["request"]["strategy"]
        estimate = json.loads(state["request"]["estimate_json"]) if state["request"]["estimate_json"] else None
        steps = []
        for attempt in state["attempts"]:
            steps.append({"attempt_id": attempt["id"], "step_key": attempt["step_key"], "status": attempt["status"],
                          "charged_units": units(attempt["charged_cents"]), "minutes": attempt["minutes"], "result": attempt["result"]})
        material_steps = [s for s in steps if s["step_key"].startswith("search:")]
        materials = self._materials(state)
        latest_script = state["latest"].get("script:" + str(strategy))
        script_succeeded = bool(latest_script and latest_script["status"] == "SUCCEEDED")
        fresh = bool(estimate and estimate["basis_version"] == state["request"]["version"])
        material_status = "PARTIAL" if material_steps and material_steps[-1]["status"] == "PARTIAL" else "SUCCEEDED" if materials else "FAILED" if material_steps else "PLANNED"
        task_plan = [] if not strategy else [
            {"step": "materials", "depends_on": [], "status": material_status,
             "dependency_satisfied": len(materials) >= self.fees["script"][strategy]["min_materials"]},
            {"step": "script:" + strategy, "depends_on": ["materials"], "status": latest_script["status"] if latest_script else "PLANNED"},
            {"step": "estimate_current", "depends_on": ["script:" + strategy], "status": "SUCCEEDED" if script_succeeded and (fresh or state["saved_plan"]) else "STALE" if script_succeeded and estimate else "PLANNED"},
            {"step": "save", "depends_on": ["script:" + strategy, "estimate_current"], "status": "SUCCEEDED" if state["saved_plan"] else state["latest"].get("save", {}).get("status", "PLANNED")},
        ]
        return {"request_id": self.request.request_id, "started_at": state["request"]["started_at"],
                "strategy": strategy, "version": state["request"]["version"], "task_plan": task_plan,
                "spent_units": units(state["spent_cents"]), "remaining_budget_units": units(self.request.budget_cents-state["spent_cents"]),
                "elapsed_minutes": state["elapsed_minutes"], "materials": materials,
                "step_attempts": steps, "latest_estimate": estimate, "estimate_is_current": fresh,
                "quotes": [self._quote(s, state) for s in STRATEGIES], "saved_plan": state["saved_plan"]}

    def digest(self):
        state = self.store.read(self.request.request_id)
        return {"strategy": state["request"]["strategy"], "version": state["request"]["version"],
                "spent_units": units(state["spent_cents"]), "elapsed_minutes": state["elapsed_minutes"],
                "material_count": len(self._materials(state))}

    def select_plan(self, strategy):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.read(self.request.request_id, db)
            if state["saved_plan"]:
                return {"ok": False, "code": "PLAN_ALREADY_SAVED"}
            if state["request"]["strategy"] is None and strategy != self.request.initial_strategy:
                return {"ok": False, "code": "INITIAL_PLAN_REQUIRED", "initial_strategy": self.request.initial_strategy,
                        "message": "assess the supplied initial draft before proposing another strategy"}
            if state["request"]["strategy"] and strategy != state["request"]["strategy"] and not state["request"]["estimate_json"]:
                return {"ok": False, "code": "INITIAL_ESTIMATE_REQUIRED", "message": "call estimate_cost for the initial draft before replanning"}
            if state["request"]["strategy"] != strategy:
                db.execute("UPDATE requests SET strategy=?,version=version+1 WHERE request_id=?", (strategy, self.request.request_id))
            return {"ok": True, "strategy": strategy, "dependencies": ["materials -> script:" + strategy, "script -> fresh_estimate -> save"],
                    "retained_attempts": len(state["attempts"]), "script_fee": self.fees["script"][strategy]}

    def estimate_cost(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.read(self.request.request_id, db)
            strategy = state["request"]["strategy"]
            if not strategy:
                return {"ok": False, "code": "PLAN_REQUIRED"}
            quote = {**self._quote(strategy, state), "basis_version": state["request"]["version"], "fees_version": self.fees["version"]}
            db.execute("UPDATE requests SET estimate_json=? WHERE request_id=?", (json.dumps(quote), self.request.request_id))
            return {"ok": True, "estimate": quote}

    def _guard(self, state, key):
        strategy = state["request"]["strategy"]
        if not strategy:
            return {"ok": False, "code": "PLAN_REQUIRED"}
        if state["saved_plan"]:
            return {"ok": False, "code": "PLAN_ALREADY_SAVED"}
        failures = sum(a["step_key"] == key and a["status"] == "FAILED" for a in state["attempts"])
        if failures >= 2:
            return {"ok": False, "code": "RETRY_LIMIT", "message": "at most two actual attempts for this failed step"}
        estimate = json.loads(state["request"]["estimate_json"]) if state["request"]["estimate_json"] else None
        if not estimate or estimate["strategy"] != strategy:
            return {"ok": False, "code": "ESTIMATE_REQUIRED"}
        quote = self._quote(strategy, state)
        if not quote["feasible"]:
            return {"ok": False, "code": "PLAN_INFEASIBLE", "estimate": quote,
                    "message": "model must replan or stop; original budget/deadline remain unchanged"}
        return None

    def _operation_guard(self, state, fee):
        if state["spent_cents"] + cents(fee["cost"]) > self.request.budget_cents:
            return {"ok": False, "code": "BUDGET_GUARD"}
        end = datetime.fromisoformat(state["request"]["started_at"]) + timedelta(minutes=state["elapsed_minutes"] + fee["minutes"])
        if end > datetime.fromisoformat(self.request.deadline):
            return {"ok": False, "code": "DEADLINE_GUARD"}
        return None

    def search_materials(self, source):
        key = "search:" + source
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.read(self.request.request_id, db)
            previous = state["latest"].get(key)
            if previous and previous["status"] in {"SUCCEEDED", "PARTIAL"}:
                return {"ok": True, "cached": True, "step_status": previous["status"], **previous["result"]}
            blocked = self._guard(state, key)
            if blocked:
                return blocked
            blocked = self._operation_guard(state, self.fees["search"][source])
            if blocked:
                return blocked
            materials = list(self.catalog[source])
            partial = source == "primary" and "search_partial" in self.faults
            if partial:
                materials = materials[:1]
            status = "PARTIAL" if partial else "SUCCEEDED" if materials else "FAILED"
            fee = self.fees["search_partial"] if partial else self.fees["search"][source]
            result = {"materials": materials, "source": source, "query": self.request.topic,
                      "warning": "secondary source unavailable; subset retained" if partial else None}
            self.store.add_attempt(db, self.request.request_id, key, status, cents(fee["cost"]), fee["minutes"], result)
            return {"ok": status != "FAILED", "code": "PARTIAL_RESULT" if partial else "NO_MATERIALS" if not materials else "FOUND",
                    "cached": False, "step_status": status, **result}

    def generate_script(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.read(self.request.request_id, db)
            strategy = state["request"]["strategy"]
            key = "script:" + str(strategy)
            previous = state["latest"].get(key)
            if previous and previous["status"] == "SUCCEEDED":
                return {"ok": True, "cached": True, "script": previous["result"]}
            blocked = self._guard(state, key)
            if blocked:
                return blocked
            materials = self._materials(state)
            if len(materials) < self.fees["script"][strategy]["min_materials"]:
                return {"ok": False, "code": "DEPENDENCY_MISSING", "required_materials": self.fees["script"][strategy]["min_materials"], "available": len(materials)}
            blocked = self._operation_guard(state, self.fees["script"][strategy])
            if blocked:
                return blocked
            fail = "generate_always" in self.faults or ("generate_once" in self.faults and self.store.fault_once(db, self.request.request_id, "generate_once"))
            fee = self.fees["script_failure"] if fail else self.fees["script"][strategy]
            if fail:
                result = {"code": "SCRIPT_TOOL_FAILED", "message": "synthetic generation service failure; retained materials are reusable"}
            else:
                result = {"script_id": hashlib.sha256((self.request.request_id+strategy).encode()).hexdigest()[:16],
                          "strategy": strategy, "material_ids": [m["id"] for m in materials],
                          "sections": [{"scene": 1, "text": self.request.topic + "：展示主题与活动目标"},
                                       {"scene": 2, "text": "用素材演示参与方式与关键利益点"},
                                       {"scene": 3, "text": "说明行动入口、活动时间和互动号召"}],
                          "generator": "synthetic-template"}
            self.store.add_attempt(db, self.request.request_id, key, "FAILED" if fail else "SUCCEEDED", cents(fee["cost"]), fee["minutes"], result)
            return {"ok": not fail, "cached": False, "code": "SCRIPT_TOOL_FAILED" if fail else "GENERATED", "result": result}

    def save_plan(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self.store.read(self.request.request_id, db)
            if state["saved_plan"]:
                return {"ok": True, "cached": True, "code": "ALREADY_SAVED", "plan": state["saved_plan"]}
            blocked = self._guard(state, "save")
            if blocked:
                return blocked
            strategy = state["request"]["strategy"]
            script = state["latest"].get("script:" + strategy)
            if not script or script["status"] != "SUCCEEDED":
                return {"ok": False, "code": "DEPENDENCY_MISSING", "dependency": "successful script"}
            estimate = json.loads(state["request"]["estimate_json"])
            if estimate["basis_version"] != state["request"]["version"]:
                return {"ok": False, "code": "FRESH_ESTIMATE_REQUIRED"}
            blocked = self._operation_guard(state, self.fees["save"])
            if blocked:
                return blocked
            fail = "save_always" in self.faults or ("save_once" in self.faults and self.store.fault_once(db, self.request.request_id, "save_once"))
            fee = self.fees["save_failure"] if fail else self.fees["save"]
            if fail:
                self.store.add_attempt(db, self.request.request_id, "save", "FAILED", cents(fee["cost"]), fee["minutes"], {"code": "SAVE_FAILED"})
                return {"ok": False, "code": "SAVE_FAILED", "message": "plan not saved; script and materials retained"}
            plan_id = str(uuid.uuid4())
            self.store.add_attempt(db, self.request.request_id, "save", "SUCCEEDED", cents(fee["cost"]), fee["minutes"], {"plan_id": plan_id})
            after = self.store.read(self.request.request_id, db)
            finish = datetime.fromisoformat(after["request"]["started_at"]) + timedelta(minutes=after["elapsed_minutes"])
            plan = {"plan_id": plan_id, "request_id": self.request.request_id, "topic": self.request.topic,
                    "strategy": strategy, "budget_units": units(self.request.budget_cents),
                    "actual_total_units": units(after["spent_cents"]), "deadline": self.request.deadline,
                    "simulated_finished_at": finish.isoformat(), "script": script["result"],
                    "material_ids": script["result"]["material_ids"], "fees_version": self.fees["version"], "published": False}
            plan["campaign"] = {"objective": "围绕“"+self.request.topic+"”制作活动内容并引导用户参与",
                                "audience_assumption": "对该主题感兴趣的用户，待运营确认",
                                "channel_assumption": "短视频与配套社交文案，待运营确认",
                                "deliverables": ["三段式活动脚本", "已检索素材清单", "步骤与费用执行记录"],
                                "suggested_metrics": ["内容完播率", "活动参与人数"],
                                "metric_boundary": "仅建议观测指标，没有实际活动效果数据"}
            db.execute("INSERT INTO plans VALUES (?, ?, ?)", (self.request.request_id, plan_id, json.dumps(plan, ensure_ascii=False)))
            verified = json.loads(db.execute("SELECT payload FROM plans WHERE request_id=?", (self.request.request_id,)).fetchone()[0])
            if verified != plan:
                raise sqlite3.DatabaseError("save verification mismatch")
            return {"ok": True, "cached": False, "code": "SAVED", "plan": verified}

    def finish(self):
        state = self.store.read(self.request.request_id)
        if state["saved_plan"]:
            return {"ok": True, "code": "FINISHED"}
        failures = [a for a in state["attempts"] if a["status"] == "FAILED"]
        candidates = [self._quote(s, state) for s in STRATEGIES]
        if not failures and any(q["feasible"] for q in candidates):
            return {"ok": False, "code": "FINISH_REJECTED", "message": "a feasible plan remains; continue tool work", "quotes": candidates}
        return {"ok": True, "code": "FINISHED_UNCOMPLETED", "quotes": candidates,
                "failed_steps": [a["step_key"] for a in failures]}

    def call(self, name, args):
        spec = next((t["function"] for t in TOOL_SCHEMAS if t["function"]["name"] == name), None)
        if not spec:
            return {"ok": False, "code": "TOOL_NOT_ALLOWED"}
        props = spec["parameters"]["properties"]
        if not isinstance(args, dict) or set(args) != set(props):
            return {"ok": False, "code": "INVALID_ARGUMENTS"}
        for key, prop in props.items():
            if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > 1000 or ("enum" in prop and args[key] not in prop["enum"]):
                return {"ok": False, "code": "INVALID_ARGUMENTS"}
        try:
            result = getattr(self, name)(**{k:v for k,v in args.items() if k != "reason"})
        except sqlite3.Error:
            result = {"ok": False, "code": "STORAGE_FAILURE", "message": "transaction did not confirm success"}
        if not result.get("ok"):
            self.last_error = result
        result["state_after"] = self.digest()
        return result

    def report(self, termination):
        state = self.state()
        saved = state["saved_plan"]
        reasons = []
        if not saved:
            if termination in {"STEP_LIMIT", "MODEL_FAILURE", "INTERRUPTED_AFTER_FAILURE"}:
                reasons.append(termination)
            if any(a["status"] == "FAILED" for a in state["step_attempts"]):
                reasons.append("TOOL_FAILURE")
            if all(not q["feasible"] for q in state["quotes"]):
                reasons.append("NO_FEASIBLE_PLAN")
            if self.last_error:
                reasons.append(self.last_error["code"])
            if not reasons:
                reasons.append("UNFINISHED_STEPS")
        return {"request_id": self.request.request_id, "status": "COMPLETED" if saved and termination == "FINISHED" else "INCOMPLETE",
                "termination": termination, "save_status": "SAVED" if saved else "NOT_SAVED",
                "task_plan": state["task_plan"], "strategy": state["strategy"],
                "budget_units": units(self.request.budget_cents), "deadline": self.request.deadline,
                "actual_total_units": state["spent_units"], "elapsed_minutes": state["elapsed_minutes"],
                "step_attempts": state["step_attempts"], "retained_materials": state["materials"],
                "saved_plan": saved, "uncompleted_reasons": list(dict.fromkeys(reasons)),
                "feasibility": state["quotes"], "published": False,
                "fee_boundary": "Synthetic business fees and virtual minutes, not actual API billing or wall-clock execution time."}


def schema(name, description, props=None):
    props = {**(props or {}), "reason": {"type": "string", "description": "One short action rationale, not private chain of thought."}}
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}, "strict": True}}


TOOL_SCHEMAS = [
    schema("select_plan", "Choose a strategy; retains all completed/failed results. Must estimate before business execution.", {"strategy": {"type": "string", "enum": list(STRATEGIES)}}),
    schema("estimate_cost", "Compute spent + remaining fees, dependencies and deadline from actual persisted state. Required again after generation and before save."),
    schema("search_materials", "Search and checkpoint synthetic assets. Partial results remain PARTIAL and may still satisfy a cheaper strategy.", {"source": {"type": "string", "enum": ["primary", "backup"]}}),
    schema("generate_script", "Generate a synthetic template script using selected strategy and enough cached materials. Failed attempts cost 1 unit / 1 minute. Maximum two attempts per strategy."),
    schema("save_plan", "Atomically save only a feasible plan with successful script and fresh estimate. Duplicate calls return same plan at no extra fee."),
    schema("finish", "End after save, or when no feasible plan or unresolved tool failure prevents completion. Premature stopping may be rejected."),
]
