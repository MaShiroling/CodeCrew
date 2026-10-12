"""Offline checks for frozen generalization replay and live orchestration."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.team.models import MemberRole
from evals import run_memory_generalization_live as generalization
from evals import run_memory_unseen_live as live
from evals.run_memory_baseline import run_case
from evals.validate_memory_generalization import validate_frozen


class _NoProposalAdapter(FakeAgentAdapter):
    async def start(self, request):
        self._scenario = FakeAgentScenario(
            output={
                "message": json.dumps(
                    {"content": "离线占位回复", "handoff_to": [], "memory_proposal": None},
                    ensure_ascii=False,
                ),
            }
        )
        return await super().start(request)


def test_smoke_selection_covers_both_domains_and_positive_negative() -> None:
    selected = live.select_cases(
        validate_frozen(), [], True, generalization.SMOKE_CASE_IDS
    )
    assert [case["id"] for case in selected] == list(generalization.SMOKE_CASE_IDS)
    assert {case["domain"] for case in selected} == {"development", "personal"}
    assert any(case["gold"]["writes"] for case in selected)
    assert any(not case["gold"]["writes"] for case in selected)


@pytest.mark.asyncio
async def test_all_12_cases_replay_without_model_calls() -> None:
    for case in validate_frozen():
        result = await run_case(
            case, _NoProposalAdapter(), agent_role=MemberRole.IMPLEMENTER
        )
        assert result["complete"], case["id"]
        expected_turns = sum(item["role"] == "human" for item in case["messages"]) + 1
        assert result["total_agent_turns"] == expected_turns, case["id"]


@pytest.mark.asyncio
async def test_live_manifest_uses_new_dataset_and_resume(tmp_path, monkeypatch) -> None:
    cases = validate_frozen()[:1]
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "test-secret-not-for-output")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODE", "on")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_API_KEY", "test-embedding-secret")
    monkeypatch.setattr(live.shutil, "which", lambda _: "/usr/bin/kimi")
    calls = []

    async def fake_run_case(case, adapter, **kwargs):
        calls.append(case["id"])
        return {
            "case_id": case["id"],
            "complete": True,
            "write_source_counts": {"tp": 0, "fp": 0, "fn": 0},
            "recall_source_counts": {"tp": 0, "fp": 0, "fn": 0},
        }

    monkeypatch.setattr(live, "run_case", fake_run_case)
    run_kwargs = {
        "output_root": tmp_path,
        "dataset_path": generalization.CASES_PATH,
        "lock_path": generalization.LOCK_PATH,
        "extra_tracked": generalization._TRACKED,
    }
    run_dir = await live.run_live(cases, **run_kwargs)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_sha256"] == json.loads(
        generalization.LOCK_PATH.read_text(encoding="utf-8")
    )["sha256"]
    assert "evals/run_memory_generalization_live.py" in manifest["code"]["sha256"]
    assert "test-secret-not-for-output" not in (run_dir / "manifest.json").read_text()
    assert "test-embedding-secret" not in (run_dir / "manifest.json").read_text()
    assert calls == [cases[0]["id"]]

    resumed = await live.run_live(cases, resume_from=run_dir, **run_kwargs)
    assert resumed == run_dir
    assert calls == [cases[0]["id"]]
