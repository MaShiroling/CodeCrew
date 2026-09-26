"""Separate opt-ins for fault-injected rework acceptance; not a quality benchmark."""

import os

import pytest

from tests.integration.test_three_agent_live import run_live_three_agent_scenario

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_THREE_AGENT_REWORK_LIVE") != "1",
    reason="enable CODECREW_RUN_THREE_AGENT_REWORK_LIVE=1 for up to seven real Agent turns",
)
async def test_live_three_agent_rework_success(tmp_path):
    await run_live_three_agent_scenario(tmp_path, scenario="rework_success")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("CODECREW_RUN_THREE_AGENT_BUDGET_LIVE") != "1",
    reason="enable CODECREW_RUN_THREE_AGENT_BUDGET_LIVE=1 for up to nine real Agent turns",
)
async def test_live_three_agent_rework_budget_exhaustion(tmp_path):
    await run_live_three_agent_scenario(tmp_path, scenario="rework_exhaustion")
