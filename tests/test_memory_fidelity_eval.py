"""The separate fidelity fixture guards what source-level baselines cannot score."""

from evals.check_memory_fidelity import evaluate_cases, load_cases, load_live_cases_v2


def test_memory_fidelity_fixture_passes_production_write_path() -> None:
    results = evaluate_cases(load_cases())
    assert len(results) == 16
    assert all(result["passed"] for result in results), results
    assert any(result["expected_content"] is None for result in results)
    assert any(result["expected_content"] is not None for result in results)


def test_v2_user_adjudication_preserves_v1_labels() -> None:
    v1 = {case["id"]: case for case in load_cases()}
    v2 = {case["id"]: case for case in load_live_cases_v2()}
    assert v1["dev_unresolved_decision"]["live_expected_write"] is False
    assert v2["dev_unresolved_decision"]["live_expected_write"] is True
    assert v2["dev_unresolved_decision"]["live_expected_kind"] == "open_loop"
    assert v1["dev_unresolved_decision"]["proposal"] == (
        v2["dev_unresolved_decision"]["proposal"]
    )
    assert [case_id for case_id in v1 if v1[case_id]["live_expected_write"]
            != v2[case_id]["live_expected_write"]] == ["dev_unresolved_decision"]
