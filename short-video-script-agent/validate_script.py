"""Recheck an editable JSON export without calling a model."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from video_script.domain import Request
from video_script.tools import BusinessTools,DRAFT_SCHEMA,SCENE_PROPERTIES,valid


def validate_export(request,script,data_dir):
    risks=[]
    allowed={"title","title_id","scenes","verification","draft_id","brief","estimated_total_seconds","scene_durations","citations","data_hash","draft_hash"}
    if not isinstance(script,dict) or set(script)!=allowed:
        return {"valid":False,"risks":["INVALID_EXPORT_SHAPE"]}
    if not isinstance(script["scenes"],list) or any(not isinstance(s,dict) or set(s)!=set(SCENE_PROPERTIES)|{"visual","narration","subtitle","part"} for s in script["scenes"]):
        return {"valid":False,"risks":["INVALID_SCENE_SHAPE"]}
    spec={"title_id":script["title_id"],"scenes":[{k:s[k] for k in SCENE_PROPERTIES} for s in script["scenes"]]}
    if not valid(spec,DRAFT_SCHEMA): return {"valid":False,"risks":["INVALID_DRAFT_SPEC"]}
    tools=BusinessTools(replace(request,initial_draft=None),data_dir)
    tools.search_materials(request.topic,"expanded")
    draft=tools.write_draft(**spec)["draft"]
    if script["title"]!=draft["rendered"]["title"] or script["scenes"]!=draft["rendered"]["scenes"]:
        risks.append("EDIT_OUTSIDE_VERIFIED_EXPRESSIONS_OR_SOURCE")
    estimate=tools.estimate_duration(draft["draft_id"])["estimate"]
    check=tools.check_rules(draft["draft_id"])["check"]
    if not estimate["passed"]: risks.append("DURATION_EXCEEDED")
    risks.extend(r["code"] for r in check["risks"])
    if script["estimated_total_seconds"]!=estimate["total_seconds"] or script["scene_durations"]!=estimate["per_scene"]:
        risks.append("DURATION_METADATA_MISMATCH")
    if script["citations"]!=check["citations"]: risks.append("CITATION_METADATA_MISMATCH")
    if script["data_hash"]!=tools.content.snapshot_hash() or script["draft_hash"]!=draft["draft_hash"]:
        risks.append("STALE_EXPORT_HASH")
    expected_brief={k:request.as_dict()[k] for k in ("topic","audience","platform","target_seconds","style")}
    if script["brief"]!=expected_brief: risks.append("BRIEF_CHANGED")
    return {"valid":not risks,"risks":list(dict.fromkeys(risks)),"duration_check":estimate,"rule_check":check,
            "boundary":"Validates the reviewed expression/source contract; arbitrary free rewrites require human review."}


def main():
    if hasattr(sys.stdout,"reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",required=True)
    parser.add_argument("--script",required=True)
    parser.add_argument("--data-dir",default="data")
    args=parser.parse_args()
    request=Request.parse(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
    script=json.loads(Path(args.script).read_text(encoding="utf-8-sig"))
    report=validate_export(request,script,args.data_dir)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report["valid"] else 2


if __name__=="__main__": raise SystemExit(main())
