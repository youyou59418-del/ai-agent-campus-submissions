"""Regenerate actual scenario outputs/traces; live mode never falls back to mock."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from material_scheduler.agent import run_agent
from material_scheduler.cli import write_json
from material_scheduler.domain import Request
from material_scheduler.models import ModelError, OfflineMock, QuotaStopGuard, build_live_model
from material_scheduler.store import Store
from material_scheduler.tools import BusinessTools


def request(case, mid="M01", hour=18, **extra):
    return {"request_id": "case-" + case, "items": [{"material_id": mid,
            "requested_slot": f"2026-11-02T{hour:02d}:00:00+08:00", "hard_requirement": True,
            "channel": "xiaoying", **extra}]}


CASES = {
    "normal": (request("normal", hour=17), "DRAFT_SAVED"),
    "missing": (request("missing", mid="M02"), "NEEDS_INFO"),
    "blocked": (request("blocked", mid="M03"), "BLOCKED"),
    "risk_review": (request("risk-review", mid="M04"), "NEEDS_REVIEW"),
    "conflict": (request("conflict"), "DRAFT_SAVED"),
    "duplicate": (request("duplicate"), "DRAFT_SAVED"),
    "save_failure": (request("save-failure", hour=17), "SAVE_FAILED"),
    "supplement": (request("supplement", mid="M07", hour=17), "DRAFT_SAVED"),
    "confirmed": (request("confirmed", approved_slot="2026-11-02T19:00:00+08:00"), "DRAFT_SAVED"),
    "changed_occupancy": (request("changed-occupancy"), "DRAFT_SAVED"),
    "step_limit": (request("step-limit"), "INCOMPLETE"),
    "no_slots": (request("no-slots"), "NO_AVAILABLE_SLOT"),
    "multi": ({"request_id": "case-multi", "items": [
        request("unused", mid="M01")["items"][0], request("unused", mid="M05")["items"][0]]}, "DRAFT_SAVED"),
}


def run_case(name, mode, out, stop_event=None):
    if stop_event and stop_event.is_set():
        return {"case": name, "mode": mode, "passed": False, "skipped": True,
                "reason": "stopped_after_quota_failure", "rounds": []}
    raw, expected = CASES[name]
    dest = out / name
    dest.mkdir(parents=True, exist_ok=True)
    write_json(dest / "input.json", raw)
    with tempfile.TemporaryDirectory(prefix="material-eval-") as tmp:
        tmp = Path(tmp)
        data = tmp / "data"
        shutil.copytree(ROOT / "data", data)
        if name == "no_slots":
            write_json(data / "schedule.json", {"occupied": [
                {"channel": "xiaoying", "slot": f"2026-11-02T{h}:00:00+08:00", "reference": f"busy-{h}"}
                for h in range(18, 22)]})
        write_json(dest / "schedule.initial.json", json.loads((data / "schedule.json").read_text(encoding="utf-8")))
        store = Store(tmp / "drafts.sqlite")
        req = Request.parse(raw)
        summaries = []
        rounds = 2 if name in {"duplicate", "changed_occupancy"} else 1
        for round_index in range(1, rounds + 1):
            if name == "changed_occupancy" and round_index == 2:
                schedule = json.loads((data / "schedule.json").read_text(encoding="utf-8"))
                old_slot = summaries[0]["suggested_slot"]
                schedule["occupied"].append({"channel": "xiaoying", "slot": old_slot, "reference": "new-occupant"})
                write_json(data / "schedule.json", schedule)
                write_json(dest / "schedule.changed.json", schedule)
            model = OfflineMock() if mode == "mock" else build_live_model()
            if mode == "live" and stop_event is not None:
                model = QuotaStopGuard(model, stop_event)
            tools = BusinessTools(req, store, data, fail_save=name == "save_failure")
            result, trace = run_agent(req, tools, model, max_steps=1 if name == "step_limit" else 24)
            write_json(dest / f"output.{round_index}.json", result)
            write_json(dest / f"trace.{round_index}.json", trace)
            item = result["items"][0]
            passed = item["outcome"] == expected and not item["published"]
            if name == "conflict" or name == "duplicate":
                passed &= bool(item["draft"] and item["draft"]["requires_confirmation"])
            if name == "confirmed":
                passed &= bool(item["draft"] and not item["draft"]["requires_confirmation"])
            if name == "duplicate" and round_index == 2:
                passed &= item["draft"]["draft_id"] == summaries[0]["draft_id"] and store.count() == 1
            if name == "changed_occupancy" and round_index == 2:
                passed &= bool(item["draft"] and item["draft"]["draft_id"] == summaries[0]["draft_id"]
                               and item["suggested_slot"] != summaries[0]["suggested_slot"] and store.count() == 1
                               and item["draft"]["requires_confirmation"])
            if name == "save_failure":
                passed &= store.count() == 0 and item["draft_save_status"] == "FAILED"
            if name == "step_limit":
                passed &= result["termination"] == "STEP_LIMIT" and result["status"] == "INCOMPLETE"
            if name == "multi":
                saved = [r["draft"] for r in result["items"]]
                passed &= all(saved) and len({d["suggested_slot"] for d in saved if d}) == 2
            summaries.append({"round": round_index, "passed": bool(passed), "expected": expected,
                              "actual": item["outcome"], "status": result["status"],
                              "termination": result["termination"], "steps": result["steps"],
                              "draft_count": store.count(), "draft_id": item["draft"]["draft_id"] if item["draft"] else None,
                              "suggested_slot": item["suggested_slot"], "model_error": result["model_error"]})
        case_summary = {"case": name, "mode": mode, "passed": all(r["passed"] for r in summaries), "rounds": summaries}
        write_json(dest / "summary.json", case_summary)
        return case_summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--case", choices=[*CASES, "all"], default="all")
    parser.add_argument("--out", default=None)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.mode == "live":
        try:
            build_live_model()
        except ModelError as exc:
            print(json.dumps({"status": "CONFIG_ERROR", "error": str(exc)}))
            return 2
    out = Path(args.out) if args.out else ROOT / "examples" / args.mode
    names = list(CASES) if args.case == "all" else [args.case]
    stop_event = Event()
    with ThreadPoolExecutor(max_workers=min(max(args.workers, 1), 4)) as pool:
        results = list(pool.map(lambda name: run_case(name, args.mode, out, stop_event), names))
    summary = {"mode": args.mode, "total_cases": len(results), "passed": sum(r["passed"] for r in results), "cases": results}
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["passed"] == summary["total_cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
