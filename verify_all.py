"""Run offline checks and audit recorded model traces; never calls a live model."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
PROJECTS=json.loads((ROOT/"projects.json").read_text(encoding="utf-8"))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--project",choices=[p["slug"] for p in PROJECTS])
    args=parser.parse_args()
    results=[]
    for project in PROJECTS:
        if args.project and project["slug"]!=args.project: continue
        folder=ROOT/project["slug"]
        report_dir=ROOT/"examples/release-verification"/project["slug"]
        report_dir.mkdir(parents=True,exist_ok=True)
        commands=[("verify",["verify.py"]),("audit-mock",["audit_evidence.py","--mode","mock"]),
                  ("audit-recorded-live",["audit_evidence.py","--mode","live"])]
        if (folder/"retrieval_benchmark.py").exists(): commands.append(("retrieval-benchmark",["retrieval_benchmark.py"]))
        if (folder/"validate_script.py").exists():
            commands.append(("validate-editable-export",["validate_script.py","--input","examples/live/overlong_repair/input.1.json","--script","examples/live/overlong_repair/output.1.script.json"]))
        command_results=[]
        for name,command in commands:
            process=subprocess.run([sys.executable,*command],cwd=folder,capture_output=True,text=True,encoding="utf-8",
                                   env={**os.environ,"PYTHONIOENCODING":"utf-8"})
            (report_dir/(name+".log")).write_text(process.stdout+process.stderr,encoding="utf-8")
            command_results.append({"name":name,"exit_code":process.returncode})
            print(f"{project['slug']}: {name} {'PASS' if process.returncode==0 else 'FAIL'}",flush=True)
        verify=json.loads((folder/"examples/verification/summary.json").read_text(encoding="utf-8"))
        live=json.loads((folder/"examples/live/summary.json").read_text(encoding="utf-8"))
        audit=json.loads((folder/"examples/verification/live-evidence-audit.json").read_text(encoding="utf-8"))
        passed=all(c["exit_code"]==0 for c in command_results) and verify["unit_tests_run"]==project["tests"]
        results.append({"project":project["slug"],"passed":passed,"commands":command_results,
                        "unit_tests":verify["unit_tests_run"],"offline_scenarios":verify["offline_scenarios_passed"],
                        "recorded_live_scenarios":live["passed"],"recorded_trace_runs":audit["trace_runs_checked"]})
    summary={"generated_at_utc":datetime.now(timezone.utc).isoformat(),"passed":bool(results) and all(r["passed"] for r in results),
             "projects":results,"unit_tests_total":sum(r["unit_tests"] for r in results),
             "offline_scenarios_total":sum(r["offline_scenarios"] for r in results),
             "recorded_live_scenarios_total":sum(r["recorded_live_scenarios"] for r in results),
             "live_api_calls_during_release_validation":0,
             "boundary":"Offline execution plus static auditing of previously recorded real model traces."}
    target=ROOT/"examples/release-verification/summary.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0 if summary["passed"] else 1


if __name__=="__main__": raise SystemExit(main())
