"""Targeted real regressions; refresh the full summary from saved per-case results."""
import argparse
import json
from pathlib import Path
from threading import Event

from evaluate import CASES, run_case

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode",choices=["mock","live"],default="live")
    parser.add_argument("cases",nargs="+",choices=list(CASES))
    args=parser.parse_args()
    out=ROOT/"examples"/args.mode
    stop=Event()
    refreshed=[]
    for name in args.cases:
        result=run_case(name,args.mode,out,stop)
        refreshed.append(result)
        print(json.dumps({"case":name,"passed":result["passed"]}),flush=True)
        if stop.is_set():
            break
    all_results=[]
    for name in CASES:
        path=out/name/"summary.json"
        if path.exists():
            all_results.append(json.loads(path.read_text(encoding="utf-8")))
    summary={"mode":args.mode,"total_cases":len(all_results),"passed":sum(r["passed"] for r in all_results),
             "cases":all_results,"collection_note":"Full suite plus named targeted regressions; individual traces carry their actual timestamps.",
             "last_refreshed_cases":[r["case"] for r in refreshed]}
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return 0 if all(r["passed"] for r in refreshed) and len(all_results)==len(CASES) else 1


if __name__=="__main__": raise SystemExit(main())
