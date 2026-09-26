"""Run the current offline suite and Ruff without enabling paid model tests."""

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

LIVE_FLAGS = (
    "CODECREW_RUN_CLI_INTEGRATION",
    "CODECREW_RUN_KIMI_LIVE",
    "CODECREW_RUN_KIMI_VERIFIER_LIVE",
    "CODECREW_RUN_DEEPSEEK_REVIEWER_LIVE",
    "CODECREW_RUN_PLANNER_KIMI_LIVE",
)
MODEL_CREDENTIALS = (
    "DEEPSEEK_API_KEY",
    "KIMI_MODEL_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def offline_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment = {name: value for name, value in source.items() if name not in MODEL_CREDENTIALS}
    environment.update(dict.fromkeys(LIVE_FLAGS, "0"))
    return environment


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    environment = offline_environment(os.environ)
    for label, command in (
        ("offline pytest", (sys.executable, "-m", "pytest", "-q")),
        ("Ruff", (sys.executable, "-m", "ruff", "check", ".")),
    ):
        print(f"Running {label} (live flags disabled)", flush=True)
        result = subprocess.run(command, cwd=root, env=environment, check=False)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
