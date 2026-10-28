import argparse
import json
from pathlib import Path
import sys

from .agent import run_agent
from .domain import Request
from .models import LazyChatModel, ModelError, OfflineMock
from .store import Store
from .tools import FAULTS


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Content campaign planning under synthetic cost/time constraints")
    parser.add_argument("--input", required=True)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--db", default=".runtime/plans.sqlite")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default=".runtime/output.json")
    parser.add_argument("--trace", default=".runtime/trace.json")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--fault", choices=FAULTS, action="append", default=[], help="Synthetic fault injection, repeatable")
    parser.add_argument("--interrupt-after-failure", action="store_true", help="Checkpoint/resume demonstration only")
    args = parser.parse_args(argv)
    try:
        request = Request.parse(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
        model = OfflineMock() if args.mode == "mock" else LazyChatModel()
        result, trace = run_agent(request, Store(args.db), args.data_dir, model,
                                  max_steps=args.max_steps, faults=args.fault, interrupt_after_failure=args.interrupt_after_failure)
        write_json(args.output, result)
        write_json(args.trace, trace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "COMPLETED" else 2
    except (ValueError, TypeError, OSError, KeyError, ModelError) as exc:
        print(json.dumps({"status": "INPUT_OR_CONFIG_ERROR", "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
