"""Independent check of citation provenance, exact excerpts and extractive rendering."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def locator(value): return (value["doc_id"],value["version_date"],value["fragment_id"])


def claim(fragment):
    if fragment["fact_key"] in {"expense.deadline","leave.notice"}:
        found=re.findall(r"(\d+)\s*(?:个)?\s*(工作日|天|小时)",fragment["text"])
        if len(found)==1: return tuple(found[0])
    return re.sub(r"\s+","",fragment["text"])


def flatten(raw):
    result={}
    for doc in raw["documents"]:
        for fragment in doc["fragments"]:
            value={**{k:v for k,v in doc.items() if k!="fragments"},**fragment}
            value["source_hash"]=digest(value)
            result[locator(value)]=value
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["mock","live"],default="live")
    args=parser.parse_args()
    base=ROOT/"examples"/args.mode
    summary=json.loads((base/"summary.json").read_text(encoding="utf-8"))
    errors,traces=[],[]
    citation_count=0
    if summary.get("total_cases")!=14 or summary.get("passed")!=14:
        errors.append("expected full passing 14-case summary")
    expected_keys={"clear_answer":"leave.notice","conflict":"expense.deadline","paraphrase":"expense.deadline",
                   "context_followup":"leave.notice","search_failure_once":"leave.notice","read_failure_once":"leave.notice",
                   "single_policy":"expense.deadline","document_replacement":"expense.deadline",
                   "conflict_disappears":"expense.deadline","stale_during_run":"leave.notice"}
    for case in summary.get("cases",[]):
        dest=base/case["case"]
        for row in case["rounds"]:
            n=row["round"]
            trace=json.loads((dest/f"trace.{n}.json").read_text(encoding="utf-8"))
            source=json.loads((dest/f"documents.{n}.after.json").read_text(encoding="utf-8"))
            before=json.loads((dest/f"documents.{n}.before.json").read_text(encoding="utf-8"))
            originals=flatten(source)
            result,steps=trace["result"],trace["steps"]
            traces.append(trace)
            prefix=f"{case['case']}/{n}: "
            if trace["mode"]!=args.mode or len(steps)!=result["steps"] or len(steps)>result["max_steps"]:
                errors.append(prefix+"mode or step bound mismatch")
            if trace["initial_corpus_hash"]!=digest(before) or result["corpus_hash"]!=digest(source):
                errors.append(prefix+"corpus snapshot hash mismatch")
            if args.mode=="live" and (trace.get("usage") or {}).get("api_calls")!=len(steps):
                errors.append(prefix+"real API response count mismatch")
            read_evidence=set()
            known_locators=set()
            accepted=None
            searches=[]
            failures=Counter()
            for i,event in enumerate(steps):
                if event["step"]!=i+1 or not event["decision_basis"]:
                    errors.append(prefix+"missing sequence or rationale")
                nxt=steps[i+1]["tool"] if i+1<len(steps) else result["termination"]
                if event["next_action"]!=nxt:
                    errors.append(prefix+"next action mismatch")
                name,obs=event["tool"],event["observation"]
                if name=="search_documents" and obs.get("ok"):
                    known_locators.update(locator(h) for h in obs["hits"])
                    searches.append(event["arguments"])
                if name=="read_fragment" and obs.get("ok"):
                    f=obs["fragment"]
                    if locator(f) not in known_locators:
                        errors.append(prefix+"read did not follow a retrieved locator")
                    read_evidence.add((locator(f),f["source_hash"],f["text"]))
                if obs.get("code") in {"SEARCH_UNAVAILABLE","READ_UNAVAILABLE","CORPUS_UNAVAILABLE"}:
                    failures[name]+=1
                if name=="submit_answer" and obs.get("ok"):
                    accepted=obs["result"]
            if result["run_status"]=="COMPLETED":
                if accepted is None or any(result[k]!=v for k,v in accepted.items()):
                    errors.append(prefix+"final result not backed by accepted tool result")
            status=result["task_status"]
            cites=result["citations"]
            if status in {"ANSWERED","CONFLICT"}:
                if not cites or result["resolved_fact_key"]!=expected_keys.get(case["case"]):
                    errors.append(prefix+"wrong fixture topic or missing citations")
                verified=[]
                for c in cites:
                    original=originals.get(locator(c))
                    if original is None or c["quote"]!=original["text"] or c["source_hash"]!=original["source_hash"] or original["kind"] not in {"policy","faq"}:
                        errors.append(prefix+"citation differs from current full original")
                        continue
                    if original["fact_key"]!=result["resolved_fact_key"]:
                        errors.append(prefix+"wrong citation fact key")
                    if (locator(c),c["source_hash"],c["quote"]) not in read_evidence:
                        errors.append(prefix+"citation was never actually read")
                    citation_count+=1
                    verified.append(original)
                peers=[f for f in originals.values() if f["fact_key"]==result["resolved_fact_key"] and f["kind"] in {"policy","faq"}]
                values={claim(f) for f in peers}
                cited_values={claim(f) for f in verified}
                lines="\n".join(f"[{c['doc_id']} / {c['version_date']} / {c['fragment_id']}] {c['quote']}" for c in cites)
                if status=="ANSWERED":
                    if len(values)!=1 or result["answer"]!="依据已核对原文：\n"+lines:
                        errors.append(prefix+"unsupported free text or hidden conflicting claim")
                else:
                    if len(values)<2 or cited_values!=values or not result["gaps"] or not result["follow_up_question"]:
                        errors.append(prefix+"incomplete conflict coverage or no clarification")
                    rendered="资料存在冲突，目前不能确定唯一适用规则：\n"+lines+"\n"+result["follow_up_question"]
                    if result["answer"]!=rendered:
                        errors.append(prefix+"unsupported text outside conflict template")
                    if not any(s["strategy"]=="expanded" and any(w in s["query"] for w in ("优先级","适用","范围","冲突")) for s in searches):
                        errors.append(prefix+"conflict authority not investigated")
            elif cites:
                errors.append(prefix+"refusal/clarification/failure invents citations")
            if status=="TOOL_FAILURE" and (not failures or max(failures.values())<2):
                errors.append(prefix+"tool failure claimed without repeated actual errors")
            if status=="INSUFFICIENT_EVIDENCE" and len(searches)<2:
                errors.append(prefix+"outside-corpus refusal without supplementary retrieval")
            if status=="INCOMPLETE" and result["answer"]:
                errors.append(prefix+"incomplete result contains unsupported facts")
    usage={k:sum((t.get("usage") or {}).get(k,0) for t in traces) for k in ("api_calls","prompt_tokens","completion_tokens")}
    report={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"mode":args.mode,"trace_runs_checked":len(traces),
            "citations_checked":citation_count,"passed":not errors,"errors":errors,"usage_for_this_suite":usage,
            "boundary":"Checks exact extractive support, controlled fixture topics and provenance; not a general semantic entailment or production accuracy proof."}
    target=ROOT/"examples/verification"/f"{args.mode}-evidence-audit.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if not errors else 1


if __name__=="__main__": raise SystemExit(main())
