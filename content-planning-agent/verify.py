"""One-command offline verification. Writes actual test evidence to examples/."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from content_planner.cli import write_json


def main():
    started = time.perf_counter()
    output = ROOT / "examples" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    (output / "unit-tests.txt").write_text(stream.getvalue(), encoding="utf-8")
    subprocess_result = subprocess.run([sys.executable, "evaluate.py", "--mode", "mock", "--case", "all"],
                                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if subprocess_result.returncode not in (0, 1):
        print("Offline scenario runner failed; inspect examples/verification/scenarios.txt")
    (output / "scenarios.txt").write_text(subprocess_result.stdout + subprocess_result.stderr, encoding="utf-8")
    summary_path = ROOT / "examples" / "mock" / "summary.json"
    scenarios = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    with tempfile.TemporaryDirectory(prefix="content-cli-") as tmp:
        tmp = Path(tmp)
        cmd = [sys.executable, "run.py", "--mode", "mock", "--input", "examples/mock/normal/input.json",
               "--db", str(tmp / "cli.sqlite"), "--output", str(tmp / "output.json"), "--trace", str(tmp / "trace.json")]
        cli = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        cli_pass = cli.returncode == 0 and json.loads((tmp / "output.json").read_text(encoding="utf-8"))["save_status"] == "SAVED"
    summary = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
               "platform": platform.system(), "unit_tests_run": result.testsRun,
               "unit_test_failures": len(result.failures), "unit_test_errors": len(result.errors),
               "unit_tests_passed": result.wasSuccessful(), "offline_scenarios_passed": scenarios.get("passed"),
               "offline_scenarios_total": scenarios.get("total_cases"), "cli_smoke_passed": cli_pass,
               "elapsed_seconds": round(time.perf_counter() - started, 3),
               "live_model_evidence": "separate under examples/live*; never inferred from these tests"}
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.wasSuccessful() and subprocess_result.returncode == 0 and cli_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
