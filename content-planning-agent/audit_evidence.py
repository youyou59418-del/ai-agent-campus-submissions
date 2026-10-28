"""Independent ledger/fee/dependency checks on saved traces; no model calls."""
import argparse
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def money(value): return Decimal(str(value))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["mock","live"],default="live")
    args=parser.parse_args()
    base=ROOT/"examples"/args.mode
    summary=json.loads((base/"summary.json").read_text(encoding="utf-8"))
    fees=json.loads((ROOT/"data/costs.json").read_text(encoding="utf-8"))
    errors,traces=[],[]
    if summary.get("total_cases")!=15 or summary.get("passed")!=15:
        errors.append("expected full 15-case passing summary")
    for case in summary.get("cases",[]):
        for row in case["rounds"]:
            trace=json.loads((base/case["case"]/f"trace.{row['round']}.json").read_text(encoding="utf-8"))
            traces.append(trace)
            prefix=f"{case['case']}/{row['round']}: "
            result,steps=trace["result"],trace["steps"]
            if trace["mode"]!=args.mode or len(steps)!=result["steps"] or len(steps)>result["max_steps"]:
                errors.append(prefix+"mode or step count mismatch")
            usage=trace.get("usage") or {}
            if args.mode=="live" and usage.get("api_calls")!=len(steps):
                errors.append(prefix+"real API response count mismatch")
            for i,event in enumerate(steps):
                expected_next=steps[i+1]["tool"] if i+1<len(steps) else result["termination"]
                if not event["decision_basis"] or event["next_action"]!=expected_next:
                    errors.append(prefix+"missing rationale or next action")
            initial=trace.get("initial_checkpoint")
            attempts=result["step_attempts"]
            if initial and attempts[:len(initial["step_attempts"])]!=initial["step_attempts"]:
                errors.append(prefix+"checkpoint history mutated or dropped")
            total=Decimal(0)
            elapsed=0
            available=set()
            scripts={}
            for attempt in attempts:
                key,status=attempt["step_key"],attempt["status"]
                if key.startswith("search:"):
                    source=key.split(":",1)[1]
                    fee=fees["search_partial"] if status=="PARTIAL" else fees["search"][source]
                    if status in {"SUCCEEDED","PARTIAL"}:
                        available.update(m["id"] for m in attempt["result"]["materials"])
                elif key.startswith("script:"):
                    strategy=key.split(":",1)[1]
                    fee=fees["script_failure"] if status=="FAILED" else fees["script"][strategy]
                    if len(available)<fees["script"][strategy]["min_materials"]:
                        errors.append(prefix+"script executed without material dependency")
                    if status=="SUCCEEDED":
                        if not set(attempt["result"]["material_ids"]).issubset(available):
                            errors.append(prefix+"script claims nonexistent material")
                        scripts[strategy]=attempt["result"]
                elif key=="save":
                    fee=fees["save_failure"] if status=="FAILED" else fees["save"]
                    if not scripts:
                        errors.append(prefix+"save executed before any script success")
                else:
                    errors.append(prefix+"unknown charged step")
                    continue
                if money(attempt["charged_units"])!=money(fee["cost"]) or attempt["minutes"]!=fee["minutes"]:
                    errors.append(prefix+"charged fee/time differs from table")
                total+=money(attempt["charged_units"])
                elapsed+=attempt["minutes"]
                if trace["input"]["budget"] is not None and total>money(trace["input"]["budget"]):
                    errors.append(prefix+"budget exceeded at an intermediate step")
                if initial and datetime.fromisoformat(initial["started_at"])+timedelta(minutes=elapsed)>datetime.fromisoformat(trace["input"]["deadline"]):
                    errors.append(prefix+"deadline exceeded at an intermediate step")
            if total!=money(result["actual_total_units"]):
                errors.append(prefix+"ledger total mismatch")
            plan=result["saved_plan"]
            nodes={n["step"]:n for n in result["task_plan"]}
            for node in nodes.values():
                if node["status"]=="SUCCEEDED":
                    for dep in node["depends_on"]:
                        dependency=nodes.get(dep,{})
                        if dependency.get("status")!="SUCCEEDED" and not (dependency.get("status")=="PARTIAL" and dependency.get("dependency_satisfied")):
                            errors.append(prefix+"successful plan node has an unsatisfied dependency")
            if bool(plan)!=(result["save_status"]=="SAVED") or result["published"]:
                errors.append(prefix+"save/publication status mismatch")
            if plan:
                if money(plan["actual_total_units"])!=total or plan["script"]!=scripts.get(plan["strategy"]):
                    errors.append(prefix+"saved plan not supported by ledger/script")
                if plan["deadline"]!=trace["input"]["deadline"] or money(plan["budget_units"])!=money(trace["input"]["budget"]):
                    errors.append(prefix+"original hard requirements altered")
                if plan["published"] or datetime.fromisoformat(plan["simulated_finished_at"])>datetime.fromisoformat(plan["deadline"]):
                    errors.append(prefix+"invalid saved plan time or publication")
            if result["status"]=="NEEDS_INPUT" and (steps or attempts or total):
                errors.append(prefix+"missing-input path consumed steps or fees")
    usage={k:sum((t.get("usage") or {}).get(k,0) for t in traces) for k in ("api_calls","prompt_tokens","completion_tokens")}
    audit={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"mode":args.mode,
           "trace_runs_checked":len(traces),"passed":not errors,"errors":errors,"usage_for_this_suite":usage,
           "fee_boundary":"Synthetic business units/minutes independently checked; API tokens are separate and do not identify free/billed usage."}
    target=ROOT/"examples/verification"/f"{args.mode}-evidence-audit.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(audit,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(audit,ensure_ascii=False,indent=2))
    return 0 if not errors else 1


if __name__=="__main__": raise SystemExit(main())
