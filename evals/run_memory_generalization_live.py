"""Replay the frozen 12-case memory generalization set through a real Kimi Agent.

Without --live, only validate case selection. A live run also uses the configured
cloud embedding endpoint, writes no keys to results, and can resume safely.
"""

import argparse
import asyncio
import getpass
import os
from pathlib import Path

from evals.run_memory_unseen_live import run_live, select_cases
from evals.validate_memory_generalization import CASES_PATH, LOCK_PATH, validate_frozen

_REPO = Path(__file__).resolve().parents[1]
_RESULTS = _REPO / "evals" / "results" / "memory-generalization-live-v1"
SMOKE_CASE_IDS = (
    "dev_timeout_fallback_scope",
    "dev_agent_grype_suggestion",
    "personal_weekday_running_boundary",
    "personal_private_exercise_format",
)
_TRACKED = (
    "evals/run_memory_generalization_live.py",
    "evals/validate_memory_generalization.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="call Kimi and cloud embedding")
    parser.add_argument("--smoke", action="store_true", help="run four fixed boundary cases")
    parser.add_argument("--case", action="append", default=[], help="select a case; repeatable")
    parser.add_argument(
        "--prompt-for-kimi-key",
        action="store_true",
        help="read Kimi Code key via hidden terminal input",
    )
    parser.add_argument("--resume-from", type=Path, help="continue a compatible, interrupted run")
    parser.add_argument("--output-root", type=Path, default=_RESULTS)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        cases = select_cases(validate_frozen(), args.case, args.smoke, SMOKE_CASE_IDS)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.live:
        print(
            f"Validated {len(cases)} frozen generalization cases; no model called. "
            "Use --live --prompt-for-kimi-key to run Kimi + embedding."
        )
        return
    if args.prompt_for_kimi_key:
        key = getpass.getpass("Kimi Code key: ").strip()
        if not key:
            parser.error("Kimi Code key cannot be empty")
        os.environ["KIMI_MODEL_API_KEY"] = key
    run_dir = asyncio.run(
        run_live(
            cases,
            output_root=args.output_root,
            resume_from=args.resume_from,
            timeout_seconds=args.timeout_seconds,
            dataset_path=CASES_PATH,
            lock_path=LOCK_PATH,
            extra_tracked=_TRACKED,
        )
    )
    print(f"Results: {run_dir}")


if __name__ == "__main__":
    main()
