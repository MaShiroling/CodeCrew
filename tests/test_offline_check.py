from types import SimpleNamespace
from unittest.mock import patch

from scripts.check_offline import LIVE_FLAGS, MODEL_CREDENTIALS, main, offline_environment


def test_three_agent_live_flag_is_explicitly_disabled() -> None:
    assert "CODECREW_RUN_THREE_AGENT_LIVE" in LIVE_FLAGS
    assert (
        offline_environment({"CODECREW_RUN_THREE_AGENT_LIVE": "1"})["CODECREW_RUN_THREE_AGENT_LIVE"]
        == "0"
    )


def test_offline_environment_disables_live_tests_and_does_not_mutate_parent() -> None:
    original = {name: "1" for name in LIVE_FLAGS}
    original.update(dict.fromkeys(MODEL_CREDENTIALS, "fake-test-secret"))
    original["PATH"] = "/usr/bin"

    environment = offline_environment(original)

    assert all(environment[name] == "0" for name in LIVE_FLAGS)
    assert not set(MODEL_CREDENTIALS) & environment.keys()
    assert environment["PATH"] == "/usr/bin"
    assert original[LIVE_FLAGS[0]] == "1"
    assert original[MODEL_CREDENTIALS[0]] == "fake-test-secret"


def test_offline_entry_runs_tests_then_ruff() -> None:
    with patch("scripts.check_offline.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        assert main() == 0

    assert run.call_count == 2
    assert run.call_args_list[0].args[0][1:] == ("-m", "pytest", "-q")
    assert run.call_args_list[1].args[0][1:] == ("-m", "ruff", "check", ".")
    for call in run.call_args_list:
        assert (call.kwargs["cwd"] / "pyproject.toml").is_file()
        assert all(call.kwargs["env"][name] == "0" for name in LIVE_FLAGS)
        assert not set(MODEL_CREDENTIALS) & call.kwargs["env"].keys()


def test_offline_entry_propagates_failure_and_stops() -> None:
    with patch("scripts.check_offline.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=7)
        assert main() == 7

    assert run.call_count == 1
