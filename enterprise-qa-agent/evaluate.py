import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from enterprise_qa.agent import run_agent
from enterprise_qa.cli import write_json
from enterprise_qa.domain import Request
from enterprise_qa.models import LazyChatModel,OfflineMock,QuotaStopGuard


def raw(name,question="年假申请需要提前几天？",history=None):
    return {"request_id":"case-"+name,"question":question,"history":history or []}


CASES={
    "clear_answer":{"input":raw("clear"),"expected":"ANSWERED","contains":"3个工作日"},
    "conflict":{"input":raw("conflict","报销期限多久？"),"expected":"CONFLICT"},
    "outside_corpus":{"input":raw("outside","明天北京天气如何？"),"expected":"INSUFFICIENT_EVIDENCE"},
    "paraphrase":{"input":raw("paraphrase","我的票据最迟啥时候交？"),"single_policy":True,"expected":"ANSWERED","contains":"30天"},
    "context_followup":{"input":raw("context","那需要提前几天？",[{"role":"user","content":"我准备申请年假。"},{"role":"assistant","content":"请继续说明你想了解的申请要求。"}]),"expected":"ANSWERED","contains":"3个工作日"},
    "context_missing":{"input":raw("context-missing","那需要提前几天？"),"expected":"NEEDS_CLARIFICATION"},
    "search_failure_once":{"input":raw("search-failure"),"faults":["search_once"],"expected":"ANSWERED","contains":"3个工作日"},
    "read_failure_once":{"input":raw("read-failure"),"faults":["read_once"],"expected":"ANSWERED","contains":"3个工作日"},
    "permanent_failure":{"input":raw("permanent"),"faults":["read_always"],"expected":"TOOL_FAILURE"},
    "single_policy":{"input":raw("single","报销期限多久？"),"single_policy":True,"expected":"ANSWERED","contains":"30天"},
    "document_replacement":{"input":raw("replacement","报销期限多久？"),"single_policy":True,"expected":"ANSWERED","rounds":2},
    "conflict_disappears":{"input":raw("disappears","报销期限多久？"),"expected":"CONFLICT","rounds":2},
    "stale_during_run":{"input":raw("stale"),"faults":["mutate_after_read"],"expected":"ANSWERED","contains":"5个工作日"},
    "step_limit":{"input":raw("limit"),"expected":"INCOMPLETE","max_steps":1},
}


def run_case(name,mode,out,stop):
    if stop.is_set(): return {"case":name,"mode":mode,"passed":False,"skipped":True,"rounds":[]}
    case=CASES[name]
    dest=out/name
    dest.mkdir(parents=True,exist_ok=True)
    write_json(dest/"input.json",case["input"])
    rows=[]
    with tempfile.TemporaryDirectory(prefix="enterprise-qa-") as tmp:
        data=Path(tmp)/"data"
        shutil.copytree(ROOT/"data",data)
        path=data/"documents.json"
        if case.get("single_policy"):
            docs=json.loads(path.read_text(encoding="utf-8"))
            docs["documents"]=[d for d in docs["documents"] if d["doc_id"]!="D02"]
            write_json(path,docs)
        earlier=None
        for round_index in range(1,case.get("rounds",1)+1):
            if round_index==2:
                docs=json.loads(path.read_text(encoding="utf-8"))
                for doc in docs["documents"]:
                    if name=="document_replacement" and doc["doc_id"]=="D01":
                        # Deliberately keep version_date unchanged to test content freshness.
                        doc["fragments"][0]["text"]=doc["fragments"][0]["text"].replace("30天","45天")
                    if name=="conflict_disappears" and doc["doc_id"]=="D02":
                        doc["fragments"][0]["text"]=doc["fragments"][0]["text"].replace("15天","30天")
                        doc["version_date"]="2026-10-20"
                write_json(path,docs)
            write_json(dest/f"documents.{round_index}.before.json",json.loads(path.read_text(encoding="utf-8")))
            model=OfflineMock() if mode=="mock" else QuotaStopGuard(LazyChatModel(),stop)
            request=Request.parse(case["input"])
            result,trace=run_agent(request,data,model,max_steps=case.get("max_steps",14),faults=case.get("faults",()))
            write_json(dest/f"documents.{round_index}.after.json",json.loads(path.read_text(encoding="utf-8")))
            write_json(dest/f"output.{round_index}.json",result)
            write_json(dest/f"trace.{round_index}.json",trace)
            expected="ANSWERED" if name=="conflict_disappears" and round_index==2 else case["expected"]
            checks={"expected_status":result["task_status"]==expected,"bounded_steps":result["steps"]<=result["max_steps"]}
            if case.get("contains"):
                checks["expected_original_fact"]=case["contains"] in result["answer"]
            if expected=="CONFLICT":
                quotes=[c["quote"] for c in result["citations"]]
                checks["both_conflicting_originals"]=any("30天" in q for q in quotes) and any("15天" in q for q in quotes)
                checks["asks_applicability"]=bool(result["follow_up_question"] and result["gaps"])
            if expected in {"INSUFFICIENT_EVIDENCE","NEEDS_CLARIFICATION","TOOL_FAILURE","INCOMPLETE"}:
                checks["no_fabricated_citation"]=not result["citations"]
            if name=="paraphrase":
                checks["query_improved"]=any(e["tool"]=="search_documents" and (e["arguments"]["strategy"]=="expanded" or e["arguments"]["query"]!=request.question) for e in trace["steps"])
            if "failure" in name:
                checks["observed_tool_error"]=any(e["observation"].get("code") in {"SEARCH_UNAVAILABLE","READ_UNAVAILABLE"} for e in trace["steps"])
            if name=="document_replacement":
                checks["fresh_fact"]=(("30天" if round_index==1 else "45天") in result["answer"])
                if round_index==2:
                    checks["changed_with_source"]=result["answer"]!=earlier["answer"] and result["corpus_hash"]!=earlier["corpus_hash"]
            if name=="conflict_disappears" and round_index==2:
                checks["conflict_not_invented"]=result["task_status"]=="ANSWERED" and "30天" in result["answer"] and result["task_status"]!=earlier["task_status"]
            if name=="stale_during_run":
                checks["read_again"]=sum(e["tool"]=="read_fragment" and e["observation"].get("ok") for e in trace["steps"])>=2
            if name=="step_limit": checks["explicit_limit"]=result["termination"]=="STEP_LIMIT"
            rows.append({"round":round_index,"passed":all(checks.values()),"checks":checks,
                         "task_status":result["task_status"],"steps":result["steps"],"model_error":result["model_error"],
                         "cited_documents":[c["doc_id"] for c in result["citations"]]})
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
