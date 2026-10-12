"""Offline checks for the frozen real-Agent replay entry point."""

import json

import pytest

from app.agents import FakeAgentAdapter, FakeAgentScenario
from app.team.models import MemberRole
from evals import run_memory_unseen_live as live
from evals.run_memory_baseline import run_case
from evals.validate_memory_unseen import validate_unseen


class _NoProposalAdapter(FakeAgentAdapter):
    async def start(self, request):
        self._scenario = FakeAgentScenario(
            output={
                "message": json.dumps(
                    {
                        "content": "离线占位回复",
                        "handoff_to": [],
                        "memory_proposal": None,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return await super().start(request)


def test_smoke_selection_covers_both_domains_and_positive_negative() -> None:
    selected = live.select_cases(validate_unseen(), [], smoke=True)
    assert [case["id"] for case in selected] == list(live.SMOKE_CASE_IDS)
    assert {case["domain"] for case in selected} == {"development", "personal"}
    assert any(case["gold"]["writes"] for case in selected)
    assert any(not case["gold"]["writes"] for case in selected)


def test_case_selection_rejects_unknown_and_duplicate_ids() -> None:
    cases = validate_unseen()
    with pytest.raises(ValueError, match="unknown case"):
        live.select_cases(cases, ["not_a_case"], smoke=False)
    with pytest.raises(ValueError, match="duplicate case"):
        live.select_cases(cases, ["dev_gateway_timeout", "dev_gateway_timeout"], smoke=False)
    with pytest.raises(ValueError, match="cannot be combined"):
        live.select_cases(cases, ["dev_gateway_timeout"], smoke=True)


@pytest.mark.asyncio
async def test_all_24_cases_replay_through_dispatch_without_model_calls() -> None:
    for case in validate_unseen():
        adapter = _NoProposalAdapter()
        result = await run_case(case, adapter, agent_role=MemberRole.IMPLEMENTER)
        assert result["complete"], case["id"]
        expected_turns = sum(item["role"] == "human" for item in case["messages"]) + 1
        assert result["total_agent_turns"] == expected_turns, case["id"]


@pytest.mark.asyncio
async def test_live_orchestration_records_results_without_secret_and_can_resume(
    tmp_path,
    monkeypatch,
) -> None:
    cases = live.select_cases(validate_unseen(), ["dev_gateway_timeout"], smoke=False)
    monkeypatch.setenv("KIMI_MODEL_API_KEY", "test-secret-not-for-output")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODE", "on")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_API_KEY", "test-embedding-secret")
    monkeypatch.setattr(live.shutil, "which", lambda _: "/usr/bin/kimi")
    calls = []

    async def fake_run_case(case, adapter, **kwargs):
        calls.append((case["id"], kwargs["variant"], kwargs["agent_role"]))
        return {
            "case_id": case["id"],
            "complete": True,
            "write_source_counts": {"tp": 1, "fp": 0, "fn": 0},
            "recall_source_counts": {"tp": 1, "fp": 0, "fn": 0},
            "answer_review": {"status": "pending_human_review"},
        }

    monkeypatch.setattr(live, "run_case", fake_run_case)
    run_dir = await live.run_live(cases, output_root=tmp_path)
    assert len(calls) == 1
    assert calls[0][1] == "hybrid"
    assert json.loads((run_dir / "summary.json").read_text())["complete_count"] == 1
    assert "test-secret-not-for-output" not in (run_dir / "manifest.json").read_text()
    assert "test-embedding-secret" not in (run_dir / "manifest.json").read_text()

    resumed = await live.run_live(cases, output_root=tmp_path, resume_from=run_dir)
    assert resumed == run_dir
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_kimi_key_fails_before_creating_results(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KIMI_MODEL_API_KEY", raising=False)
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODE", "on")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_MODEL", "test-embedding")
    monkeypatch.setenv("CODECREW_MEMORY_EMBEDDING_API_KEY", "test-embedding-secret")
    monkeypatch.setattr(live.shutil, "which", lambda _: "/usr/bin/kimi")
    with pytest.raises(RuntimeError, match="KIMI_MODEL_API_KEY is required"):
        await live.run_live(validate_unseen()[:1], output_root=tmp_path)
    assert list(tmp_path.iterdir()) == []
