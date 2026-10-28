import argparse
import json
from pathlib import Path
import sys

from .agent import run_agent
from .domain import Request
from .models import LazyChatModel,OfflineMock
from .tools import FAULTS


def write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def main(argv=None):
    if hasattr(sys.stdout,"reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="Evidence-checked synthetic enterprise knowledge QA")
    parser.add_argument("--input",required=True)
    parser.add_argument("--mode",choices=["mock","live"],default="mock")
    parser.add_argument("--data-dir",default="data")
    parser.add_argument("--output",default=".runtime/output.json")
    parser.add_argument("--trace",default=".runtime/trace.json")
    parser.add_argument("--max-steps",type=int,default=14)
    parser.add_argument("--fault",choices=FAULTS,action="append",default=[])
    args=parser.parse_args(argv)
    try:
        request=Request.parse(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
        model=OfflineMock() if args.mode=="mock" else LazyChatModel()
        result,trace=run_agent(request,args.data_dir,model,max_steps=args.max_steps,faults=args.fault)
        write_json(args.output,result)
        write_json(args.trace,trace)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result["run_status"]=="COMPLETED" else 2
    except (ValueError,TypeError,OSError,KeyError) as exc:
        print(json.dumps({"run_status":"INPUT_OR_CONFIG_ERROR","error_type":type(exc).__name__}),file=sys.stderr)
        return 2


if __name__=="__main__": raise SystemExit(main())
