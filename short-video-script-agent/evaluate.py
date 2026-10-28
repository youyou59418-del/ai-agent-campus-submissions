import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from video_script.agent import run_agent
from video_script.cli import export_result,write_json
from video_script.domain import Request
from video_script.models import LazyChatModel,OfflineMock,QuotaStopGuard,basic_spec


def raw(name,**extra):
    return {"request_id":"case-"+name,"topic":"字幕功能介绍","audience":"新手创作者","platform":"抖音","target_seconds":30,"style":"自然",**extra}


def initial(holds=(14,12,12),style="自然"):
    request=Request.parse(raw("seed"))
    return basic_spec(request,{"source_id":"P01","fact_id":"F1","text":"支持字幕。"},list(holds),style)


CASES={
    "normal":{"input":raw("normal"),"expected":"SCRIPT_READY"},
    "missing_audience":{"input":raw("missing-audience",audience=None),"expected":"NEEDS_INPUT"},
    "missing_duration":{"input":raw("missing-duration",target_seconds=None),"expected":"NEEDS_INPUT"},
    "missing_multiple":{"input":raw("missing-multiple",topic=None,platform=None,style=None),"expected":"NEEDS_INPUT"},
    "no_materials":{"input":raw("no-materials"),"remove_source":True,"expected":"NEEDS_MATERIALS"},
    "query_rewrite":{"input":raw("rewrite",topic="给画面加字"),"expected":"SCRIPT_READY"},
    "search_failure_once":{"input":raw("search-failure"),"faults":["search_once"],"expected":"SCRIPT_READY"},
    "estimate_failure_once":{"input":raw("estimate-failure"),"faults":["estimate_once"],"expected":"SCRIPT_READY"},
    "rules_permanent_failure":{"input":raw("rules-failure"),"faults":["rules_always"],"expected":"TOOL_FAILURE"},
    "overlong_repair":{"input":raw("overlong",initial_draft=initial()),"expected":"SCRIPT_READY"},
    "impossible_duration":{"input":raw("impossible",target_seconds=5),"expected":"UNFULFILLED"},
    "brand_repair":{"input":raw("brand",initial_draft=initial((6,4,6),"专业")),"expected":"SCRIPT_READY"},
    "unsupported_claim":{"input":raw("unsupported",required_claims=["效率提升80%"]),"expected":"UNFULFILLED"},
    "target_changed":{"input":raw("target-changed",initial_draft=initial()),"rounds":2,"expected":"SCRIPT_READY"},
    "retrieval_changed":{"input":raw("retrieval-changed"),"rounds":2,"expected":"SCRIPT_READY"},
    "step_limit":{"input":raw("step-limit"),"max_steps":1,"expected":"INCOMPLETE"},
}


def snapshot(data):
    return {name:json.loads((data/name).read_text(encoding="utf-8")) for name in ("materials.json","brand_rules.json","expressions.json")}


def run_case(name,mode,out,stop):
    if stop.is_set(): return {"case":name,"mode":mode,"passed":False,"skipped":True,"rounds":[]}
    case=CASES[name]
    dest=out/name
    dest.mkdir(parents=True,exist_ok=True)
    rows=[]
    with tempfile.TemporaryDirectory(prefix="video-script-") as tmp:
        data=Path(tmp)/"data"
        shutil.copytree(ROOT/"data",data)
        if case.get("remove_source"):
            docs=json.loads((data/"materials.json").read_text(encoding="utf-8"))
            docs["materials"]=[m for m in docs["materials"] if m["source_id"]!="P01"]
            write_json(data/"materials.json",docs)
        earlier=None
        for n in range(1,case.get("rounds",1)+1):
            raw_input=json.loads(json.dumps(case["input"],ensure_ascii=False))
            expected=case["expected"]
            if n==2 and name=="target_changed": raw_input["target_seconds"]=20
            if n==2 and name=="retrieval_changed":
                docs=json.loads((data/"materials.json").read_text(encoding="utf-8"))
                docs["materials"]=[m for m in docs["materials"] if m["source_id"]!="P01"]
                write_json(data/"materials.json",docs)
                expected="NEEDS_MATERIALS"
            request=Request.parse(raw_input)
            write_json(dest/f"input.{n}.json",raw_input)
            write_json(dest/f"data.{n}.before.json",snapshot(data))
            model=OfflineMock() if mode=="mock" else QuotaStopGuard(LazyChatModel(),stop)
            result,trace=run_agent(request,data,model,max_steps=case.get("max_steps",18),faults=case.get("faults",()))
            export_result(dest/f"output.{n}.json",result)
            write_json(dest/f"trace.{n}.json",trace)
            write_json(dest/f"data.{n}.after.json",snapshot(data))
            script=result["script"]
            checks={"expected_status":result["task_status"]==expected,"bounded_steps":result["steps"]<=result["max_steps"],"never_published":result["published"] is False}
            if script:
                checks["duration_within_target"]=script["estimated_total_seconds"]<=request.target_seconds
                checks["scene_time_sum"]=sum(s["estimated_seconds"] for s in script["scene_durations"])==script["estimated_total_seconds"]
                checks["has_source_citation"]=bool(script["citations"]) and all(c["source_id"]=="P01" and c["quote"]=="支持字幕。" for c in script["citations"])
                checks["no_invented_performance_fact"]="80%" not in script["title"]+"".join(s["narration"] for s in script["scenes"])
            else: checks["no_unapproved_script_delivered"]=script is None
            if expected=="NEEDS_INPUT": checks["no_model_calls"]=result["steps"]==0
            if name in {"overlong_repair","target_changed"}:
                initial_check=next((e for e in result["duration_checks"] if e["draft_id"]=="D0"),None)
                checks["observed_initial_38"]=bool(initial_check and initial_check["total_seconds"]==38 and not initial_check["passed"])
                checks["actually_revised"]=bool(script and script["draft_id"]!="D0")
            if name=="brand_repair": checks["brand_fail_then_pass"]=bool(result["rule_checks"] and not result["rule_checks"][0]["passed"] and result["rule_checks"][-1]["passed"])
            if name=="unsupported_claim": checks["unsupported_requirement_reported"]=any(r["code"]=="REQUESTED_FACT_NOT_SUPPORTED" for r in result["risks"])
            if "failure" in name: checks["observed_tool_error"]=any(e["observation"].get("code") in {"SEARCH_UNAVAILABLE","ESTIMATE_UNAVAILABLE","RULES_UNAVAILABLE"} for e in trace["steps"])
            if name=="target_changed" and n==2:
                checks["new_target_is_binding"]=script is not None and script["brief"]["target_seconds"]==20
                checks["response_to_changed_target"]=bool(script and earlier["script"] and script["estimated_total_seconds"]!=earlier["script"]["estimated_total_seconds"])
            if name=="retrieval_changed" and n==2: checks["response_to_missing_source"]=script is None and earlier["script"] is not None
            if name=="step_limit": checks["explicit_limit"]=result["termination"]=="STEP_LIMIT"
            rows.append({"round":n,"passed":all(checks.values()),"checks":checks,"task_status":result["task_status"],
                         "steps":result["steps"],"model_error":result["model_error"],
                         "estimated_total_seconds":script["estimated_total_seconds"] if script else None})
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
        results=list(pool.map(lambda n:run_case(n,args.mode,out,stop),names))
    summary={"mode":args.mode,"total_cases":len(results),"passed":sum(r["passed"] for r in results),"cases":results}
    write_json(out/"summary.json",summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if summary["passed"]==len(results) else 1


if __name__=="__main__": raise SystemExit(main())
