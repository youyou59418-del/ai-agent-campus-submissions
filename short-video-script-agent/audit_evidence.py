"""Recompute timing and source/phrase provenance independently of model prose."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent


def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def units(text): return sum(1 if re.fullmatch(r"[\u4e00-\u9fff]",word) else 2 for word in re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+",text))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["mock","live"],default="live")
    args=parser.parse_args()
    base=ROOT/"examples"/args.mode
    summary=json.loads((base/"summary.json").read_text(encoding="utf-8"))
    errors,traces=[],[]
    checked_scenes=checked_citations=approved_count=duration_records=0
    if summary.get("total_cases")!=16 or summary.get("passed")!=16: errors.append("expected full passing 16-case summary")
    for case in summary.get("cases",[]):
        dest=base/case["case"]
        for row in case["rounds"]:
            n=row["round"]
            trace=json.loads((dest/f"trace.{n}.json").read_text(encoding="utf-8"))
            data=json.loads((dest/f"data.{n}.after.json").read_text(encoding="utf-8"))
            result,steps=trace["result"],trace["steps"]
            request=trace["input"]
            traces.append(trace)
            prefix=f"{case['case']}/{n}: "
            materials,rules,expressions=(data[k] for k in ("materials.json","brand_rules.json","expressions.json"))
            data_hash=digest((materials,rules,expressions))
            if result["published"] or trace["mode"]!=args.mode or len(steps)!=result["steps"] or len(steps)>result["max_steps"]:
                errors.append(prefix+"publication/mode/step bound mismatch")
            if args.mode=="live" and (trace.get("usage") or {}).get("api_calls")!=len(steps):
                errors.append(prefix+"actual API response count mismatch")
            retrieved=set()
            estimate_events={}
            rule_events={}
            submit_event=None
            failures=Counter()
            for i,event in enumerate(steps):
                if event["step"]!=i+1 or not event["decision_basis"]:
                    errors.append(prefix+"missing step sequence or rationale")
                nxt=steps[i+1]["tool"] if i+1<len(steps) else result["termination"]
                if event["next_action"]!=nxt: errors.append(prefix+"next action mismatch")
                name,obs=event["tool"],event["observation"]
                if name=="search_materials" and obs.get("ok"):
                    retrieved.update((f["source_id"],f["fact_id"],f["source_hash"]) for f in obs["hits"])
                if name=="estimate_duration" and obs.get("ok"): estimate_events[obs["estimate"]["draft_id"]]=obs["estimate"]
                if name=="check_rules" and obs.get("ok"): rule_events[obs["check"]["draft_id"]]=obs["check"]
                if name=="submit_script" and obs.get("ok"): submit_event=obs["script"]
                if obs.get("code") in {"SEARCH_UNAVAILABLE","ESTIMATE_UNAVAILABLE","RULES_UNAVAILABLE","DATA_UNAVAILABLE"}: failures[name]+=1
            drafts={d["draft_id"]:d for d in result["drafts"]}
            for draft in drafts.values():
                if draft["draft_hash"]!=digest({"spec":draft["spec"],"rendered":draft["rendered"]}): errors.append(prefix+"draft content hash mismatch")
            computed={}
            for estimate in result["duration_checks"]:
                duration_records+=1
                draft=drafts.get(estimate["draft_id"])
                if draft is None:
                    errors.append(prefix+"estimate refers to missing draft")
                    continue
                per=[]
                for scene in draft["rendered"]["scenes"]:
                    count=units(scene["narration"])
                    speech=(count+rules["speech_units_per_second"]-1)//rules["speech_units_per_second"]
                    per.append({"scene_id":scene["scene_id"],"speech_units":count,"speech_seconds":speech,
                                "hold_seconds":scene["hold_seconds"],"estimated_seconds":max(speech,scene["hold_seconds"],rules["minimum_scene_seconds"])})
                total=sum(s["estimated_seconds"] for s in per)
                if estimate["per_scene"]!=per or estimate["total_seconds"]!=total or estimate["target_seconds"]!=request["target_seconds"] or estimate["passed"]!=(total<=request["target_seconds"]):
                    errors.append(prefix+"duration calculation/target mismatch")
                if estimate_events.get(estimate["draft_id"])!=estimate: errors.append(prefix+"duration report not backed by actual tool event")
                computed[estimate["draft_id"]]=(per,total)
            script=result["script"]
            if script:
                approved_count+=1
                key=script["draft_id"]
                estimate=estimate_events.get(key,{})
                check=rule_events.get(key,{})
                if submit_event!=script or not estimate.get("passed") or not check.get("passed"):
                    errors.append(prefix+"approved script lacks current passing checks/submit event")
                if script["data_hash"]!=data_hash or estimate.get("data_hash")!=data_hash or check.get("data_hash")!=data_hash:
                    errors.append(prefix+"checks not bound to the current data snapshot")
                if key not in drafts or any(v.get("draft_hash")!=script["draft_hash"] for v in (estimate,check)):
                    errors.append(prefix+"checks from a different revision")
                per,total=computed.get(key,([],0))
                if script["scene_durations"]!=per or script["estimated_total_seconds"]!=total or total>request["target_seconds"]:
                    errors.append(prefix+"final scene times do not sum to allowed total")
                brief={k:request[k] for k in ("topic","audience","platform","target_seconds","style")}
                if script["brief"]!=brief: errors.append(prefix+"original brief changed")
                title=expressions["titles"].get(script["title_id"],{})
                if title.get("text")!=script["title"] or title.get("style")!=request["style"]: errors.append(prefix+"unreviewed title or style")
                if [s["scene_id"] for s in script["scenes"]]!=list(range(1,len(script["scenes"])+1)) or not rules["minimum_scenes"]<=len(script["scenes"])<=rules["maximum_scenes"]:
                    errors.append(prefix+"invalid scene structure")
                relevant={k for k,t in materials["topics"].items() if any(a in request["topic"] for a in t["aliases"])}
                expected_cites=[]
                parts=set()
                for scene in script["scenes"]:
                    checked_scenes+=1
                    if scene["visual"]!=expressions["visuals"].get(scene["visual_id"]): errors.append(prefix+"unsupported visual description")
                    if scene["speech_id"]=="FACT":
                        source=next((m for m in materials["materials"] if m["source_id"]==scene["source_id"]),None)
                        fact=next((f for f in source["facts"] if f["fact_id"]==scene["fact_id"]),None) if source else None
                        if not source or not fact:
                            errors.append(prefix+"nonexistent factual source")
                            continue
                        value={k:source[k] for k in ("source_id","version_date","topic_key","title")}|dict(fact)
                        source_hash=digest(value)
                        if not fact["approved"] or source["topic_key"] not in relevant or scene["source_quote"]!=fact["text"] or scene["narration"]!=fact["text"] or scene["subtitle"]!=fact["text"]:
                            errors.append(prefix+"unsupported or unrelated product fact")
                        if (source["source_id"],fact["fact_id"],source_hash) not in retrieved: errors.append(prefix+"fact not actually retrieved")
                        expected_cites.append({"scene_id":scene["scene_id"],"source_id":source["source_id"],"fact_id":fact["fact_id"],"version_date":source["version_date"],"quote":fact["text"],"source_hash":source_hash})
                        parts.add("fact")
                        checked_citations+=1
                    else:
                        phrase=expressions["speech"].get(scene["speech_id"],{})
                        if phrase.get("text")!=scene["narration"] or phrase.get("text")!=scene["subtitle"] or phrase.get("style")!=request["style"]:
                            errors.append(prefix+"unreviewed creative narration")
                        if scene["source_id"] or scene["fact_id"] or scene["source_quote"]: errors.append(prefix+"factual fields smuggled into creative scene")
                        parts.add(phrase.get("part"))
                    if any(p in scene["narration"] or p in scene["visual"] for p in rules["forbidden_phrases"]): errors.append(prefix+"forbidden brand claim")
                if not set(rules["required_parts"]).issubset(parts) or not expected_cites: errors.append(prefix+"missing required script parts or citations")
                if script["citations"]!=expected_cites or check.get("citations")!=expected_cites: errors.append(prefix+"scene-to-source citation mapping mismatch")
                normalized={c["quote"].rstrip("。.!！ ") for c in expected_cites}
                if any(c.rstrip("。.!！ ") not in normalized for c in request["required_claims"]): errors.append(prefix+"silently dropped required factual claim")
                if any(p in script["title"] for p in rules["forbidden_phrases"]): errors.append(prefix+"forbidden title claim")
                if result["task_status"]!="SCRIPT_READY": errors.append(prefix+"script returned with non-ready status")
            elif result["task_status"]=="SCRIPT_READY": errors.append(prefix+"ready status without approved script")
            if result["task_status"]=="NEEDS_INPUT" and (steps or drafts): errors.append(prefix+"missing input consumed model actions")
            if result["task_status"]=="TOOL_FAILURE" and (not failures or max(failures.values())<2): errors.append(prefix+"tool failure without repeated observed errors")
            exported=json.loads((dest/f"output.{n}.script.json").read_text(encoding="utf-8"))
            if script and exported!=script or not script and exported.get("script","not-null") is not None:
                errors.append(prefix+"public script export is stale or mismatched")
    usage={k:sum((t.get("usage") or {}).get(k,0) for t in traces) for k in ("api_calls","prompt_tokens","completion_tokens")}
    report={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"mode":args.mode,"trace_runs_checked":len(traces),
            "approved_scripts_checked":approved_count,"approved_scenes_checked":checked_scenes,"fact_citations_checked":checked_citations,
            "duration_records_recomputed":duration_records,"passed":not errors,"errors":errors,"usage_for_this_suite":usage,
            "boundary":"Checks reviewed expression/source contract and deterministic timing; not general semantic factuality or measured TTS/video duration."}
    path=ROOT/"examples/verification"/f"{args.mode}-evidence-audit.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if not errors else 1


if __name__=="__main__": raise SystemExit(main())
