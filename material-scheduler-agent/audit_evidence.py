"""Check saved evidence independently of model prose; performs no network calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from material_scheduler.cli import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "mock"], default="live")
    args = parser.parse_args()
    base = ROOT / "examples" / args.mode
    summary = json.loads((base / "summary.json").read_text(encoding="utf-8"))
    errors, traces = [], []
    if summary.get("passed") != 13 or summary.get("total_cases") != 13:
        errors.append("expected full 13-case passing summary")
    for case in summary.get("cases", []):
        for run in case["rounds"]:
            p = base / case["case"] / f"trace.{run['round']}.json"
            trace = json.loads(p.read_text(encoding="utf-8"))
            traces.append(trace)
            prefix = f"{case['case']}/{run['round']}: "
            result = trace["result"]
            if trace["mode"] != args.mode or result["mode"] != args.mode:
                errors.append(prefix + "wrong mode")
            if len(trace["steps"]) != result["steps"] or len(trace["steps"]) > result["max_steps"]:
                errors.append(prefix + "step bound/count mismatch")
            reviewed, queried, saved = {}, {}, {}
            for index, event in enumerate(trace["steps"], 1):
                name, params, obs = event["tool"], event["arguments"], event["observation"]
                mid = params.get("material_id")
                if event["step"] != index or not event["decision_basis"]:
                    errors.append(prefix + "step sequence or rationale missing")
                if name == "publish":
                    errors.append(prefix + "forbidden publication")
                if name == "read_material":
                    reviewed.pop(mid, None)
                if name == "check_rules" and obs.get("ok"):
                    reviewed[mid] = obs.get("review_status")
                if name == "query_slots" and obs.get("ok"):
                    queried.setdefault(mid, set()).update(s["slot"] for s in obs["slots"])
                if name == "save_draft" and obs.get("ok"):
                    if reviewed.get(mid) != "APPROVED" or params["slot"] not in queried.get(mid, set()):
                        errors.append(prefix + "saved without prior review/query")
                    saved[mid] = obs["draft"]
                expected_next = trace["steps"][index]["tool"] if index < len(trace["steps"]) else result["termination"]
                if event["next_action"] != expected_next:
                    errors.append(prefix + "next action mismatch")
            inputs = {i["material_id"]: i for i in trace["input"]["items"]}
            for item in result["items"]:
                if item["published"]:
                    errors.append(prefix + "published flag true")
                draft = item["draft"]
                if draft:
                    original = inputs[item["material_id"]]
                    if saved.get(item["material_id"]) != draft or draft["published"]:
                        errors.append(prefix + "draft is not supported by successful tool result")
                    if draft["requested_slot"] != original["requested_slot"]:
                        errors.append(prefix + "hard original time overwritten")
                    alternative = draft["suggested_slot"] != original["requested_slot"]
                    if alternative and original["hard_requirement"] and not original.get("approved_slot"):
                        if not draft["requires_confirmation"] or draft["status"] != "PENDING_CONFIRMATION":
                            errors.append(prefix + "unauthorized alternative confirmation")
                if item["outcome"] == "SAVE_FAILED" and item["draft_save_status"] != "FAILED":
                    errors.append(prefix + "save failure misreported")
            if args.mode == "live" and (not trace.get("usage") or trace["usage"]["api_calls"] != len(trace["steps"])):
                errors.append(prefix + "missing or inconsistent real API usage")
    usage = {k: sum((t.get("usage") or {}).get(k, 0) for t in traces)
             for k in ("api_calls", "prompt_tokens", "completion_tokens")}
    record = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "trace_runs_checked": len(traces), "errors": errors, "passed": not errors,
              "usage_for_this_suite": usage,
              "usage_boundary": "API-reported usage for saved traces of this suite only; not free/billed classification or full account usage."}
    write_json(ROOT / "examples" / "verification" / f"{args.mode}-evidence-audit.json", record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
