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
from content_planner.agent import run_agent
from content_planner.cli import write_json
from content_planner.domain import Request, cents
from content_planner.models import LazyChatModel, OfflineMock, QuotaStopGuard
from content_planner.store import Store


def raw(name, budget=20, deadline="2026-11-03T09:30:00+08:00", **extra):
    return {"request_id": "case-"+name, "topic": "秋日创作挑战活动", "budget": budget,
            "deadline": deadline, "start_time": "2026-11-03T09:00:00+08:00", "initial_strategy": "premium", **extra}


CASES = {
    "normal": {"input": raw("normal"), "expected": "COMPLETED"},
    "missing_budget": {"input": raw("missing-budget", budget=None), "expected": "NEEDS_INPUT"},
    "missing_deadline": {"input": raw("missing-deadline", deadline=None), "expected": "NEEDS_INPUT"},
    "missing_both": {"input": raw("missing-both", budget=None, deadline=None), "expected": "NEEDS_INPUT"},
    "tool_failure": {"input": raw("tool-failure"), "faults": ["generate_once"], "expected": "COMPLETED"},
    "budget_replan": {"input": raw("budget-replan", budget=10), "expected": "COMPLETED"},
    "budget_impossible": {"input": raw("budget-impossible", budget=5), "expected": "INCOMPLETE"},
    "deadline_replan": {"input": raw("deadline-replan", deadline="2026-11-03T09:08:00+08:00"), "expected": "COMPLETED"},
    "deadline_impossible": {"input": raw("deadline-impossible", deadline="2026-11-03T09:04:00+08:00"), "expected": "INCOMPLETE"},
    "partial_success": {"input": raw("partial", budget=12), "faults": ["search_partial"], "expected": "COMPLETED"},
    "permanent_failure": {"input": raw("permanent"), "faults": ["generate_always"], "expected": "INCOMPLETE"},
    "duplicate": {"input": raw("duplicate"), "expected": "COMPLETED", "rounds": 2},
    "resume": {"input": raw("resume", budget=12), "faults": ["generate_once"], "expected": "COMPLETED", "rounds": 2},
    "save_failure": {"input": raw("save-failure"), "faults": ["save_always"], "expected": "INCOMPLETE"},
    "step_limit": {"input": raw("step-limit"), "expected": "INCOMPLETE", "max_steps": 1},
}


def run_case(name, mode, out, stop_event):
    if stop_event.is_set():
        return {"case": name, "mode": mode, "passed": False, "skipped": True, "rounds": []}
    case = CASES[name]
    dest = out/name
    dest.mkdir(parents=True, exist_ok=True)
    write_json(dest/"input.json", case["input"])
    rows, earlier = [], None
    with tempfile.TemporaryDirectory(prefix="content-eval-") as tmp:
        store = Store(Path(tmp)/"plans.sqlite")
        data = Path(tmp)/"data"
        shutil.copytree(ROOT/"data", data)
        for round_index in range(1, case.get("rounds",1)+1):
            request = Request.parse(case["input"])
            model = OfflineMock() if mode == "mock" else QuotaStopGuard(LazyChatModel(), stop_event)
            result, trace = run_agent(request, store, data, model, max_steps=case.get("max_steps",24),
                                      faults=case.get("faults",()), interrupt_after_failure=name=="resume" and round_index==1)
            write_json(dest/f"output.{round_index}.json", result)
            write_json(dest/f"trace.{round_index}.json", trace)
            expected = "INCOMPLETE" if name=="resume" and round_index==1 else case["expected"]
            checks = {"expected_status": result["status"]==expected, "never_published": result["published"] is False,
                      "ledger_sum": sum(cents(s["charged_units"]) for s in result["step_attempts"])==cents(result["actual_total_units"])}
            if request.budget_cents is not None:
                checks["budget_not_exceeded"] = cents(result["actual_total_units"]) <= request.budget_cents
            plan = result["saved_plan"]
            if plan:
                checks["deadline_not_exceeded"] = plan["simulated_finished_at"] <= plan["deadline"]
                checks["save_matches_spent"] = plan["actual_total_units"] == result["actual_total_units"]
            if expected=="NEEDS_INPUT":
                checks["no_model_calls"] = result["steps"]==0 and cents(result["actual_total_units"])==0
            attempts = result["step_attempts"]
            if name=="budget_replan":
                estimates=[s["observation"]["estimate"]["estimated_total_units"] for s in trace["steps"] if s["tool"]=="estimate_cost" and s["observation"].get("ok")]
                checks["observed_12_then_8"] = "12.00" in estimates and "8.00" in estimates and estimates.index("12.00") < estimates.index("8.00")
                checks["actual_8"] = result["actual_total_units"]=="8.00" and result["strategy"]=="economy"
            if name in {"tool_failure","resume"}:
                checks["failed_attempt_retained"] = any(a["status"]=="FAILED" for a in attempts)
                checks["successful_search_not_reexecuted"] = sum(a["step_key"]=="search:primary" for a in attempts)==1
            if name=="partial_success":
                checks["partial_not_faked"] = any(a["step_key"]=="search:primary" and a["status"]=="PARTIAL" for a in attempts)
                checks["adjusted_strategy"] = result["strategy"]=="economy"
            if name in {"permanent_failure","save_failure"}:
                checks["failure_not_saved"] = plan is None and any(a["status"]=="FAILED" for a in attempts)
            if name=="duplicate" and round_index==2:
                checks["one_same_plan"] = bool(plan and earlier["saved_plan"] and plan["plan_id"]==earlier["saved_plan"]["plan_id"])
                checks["no_new_charge_or_steps"] = attempts==earlier["step_attempts"] and result["actual_total_units"]==earlier["actual_total_units"]
            if name=="resume" and round_index==2:
                checks["kept_first_attempts"] = attempts[:len(earlier["step_attempts"])]==earlier["step_attempts"]
                checks["replanned_after_failure"] = result["strategy"]=="economy" and result["actual_total_units"]=="9.00"
            if name=="step_limit":
                checks["bounded_unfinished"] = result["termination"]=="STEP_LIMIT" and result["steps"]==1
            rows.append({"round":round_index,"passed":all(checks.values()),"checks":checks,"actual":result["status"],
                         "termination":result["termination"],"strategy":result.get("strategy"),
                         "spent_units":result["actual_total_units"],"steps":result["steps"],
                         "plan_id":plan["plan_id"] if plan else None,"model_error":result["model_error"]})
            earlier=result
    summary={"case":name,"mode":mode,"passed":all(r["passed"] for r in rows),"rounds":rows}
    write_json(dest/"summary.json",summary)
    return summary


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["mock","live"],default="mock")
    parser.add_argument("--case",choices=[*CASES,"all"],default="all")
    parser.add_argument("--out")
    parser.add_argument("--workers",type=int,default=1)
    args=parser.parse_args()
    out=Path(args.out) if args.out else ROOT/"examples"/args.mode
    names=list(CASES) if args.case=="all" else [args.case]
    stop=Event()
    with ThreadPoolExecutor(max_workers=min(max(args.workers,1),3)) as pool:
        results=list(pool.map(lambda name:run_case(name,args.mode,out,stop),names))
    summary={"mode":args.mode,"total_cases":len(results),"passed":sum(r["passed"] for r in results),"cases":results}
    write_json(out/"summary.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if summary["passed"]==len(results) else 1


if __name__=="__main__":
    raise SystemExit(main())
