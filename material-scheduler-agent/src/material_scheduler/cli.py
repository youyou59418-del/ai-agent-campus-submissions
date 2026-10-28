import argparse
import json
from pathlib import Path
import sys

from .agent import run_agent
from .domain import Request
from .models import ModelError, OfflineMock, build_live_model
from .store import Store
from .tools import BusinessTools


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review materials and save local scheduling drafts. Never publishes.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--db", default=".runtime/drafts.sqlite")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default=".runtime/result.json")
    parser.add_argument("--trace", default=".runtime/trace.json")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--simulate-save-failure", action="store_true", help="Synthetic acceptance-test fault injection")
    args = parser.parse_args(argv)
    try:
        request = Request.parse(json.loads(Path(args.input).read_text(encoding="utf-8-sig")))
        model = OfflineMock() if args.mode == "mock" else build_live_model()
        tools = BusinessTools(request, Store(args.db), Path(args.data_dir), fail_save=args.simulate_save_failure)
        result, trace = run_agent(request, tools, model, max_steps=args.max_steps)
        write_json(args.output, result)
        write_json(args.trace, trace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "COMPLETED" else 2
    except (ValueError, TypeError, OSError, KeyError, ModelError) as exc:
        # No provider response bodies, headers, or secret values are printed.
        error = {"status": "INPUT_OR_CONFIG_ERROR", "error_type": type(exc).__name__}
        if isinstance(exc, ModelError):
            error["error"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
