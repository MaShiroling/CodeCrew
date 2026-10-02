import pytest
from pydantic import ValidationError

from app.team import MemberRole, PersonaProfile, TeamPersonaCatalog, default_team_personas


def test_default_catalog_has_three_distinct_role_bound_profiles() -> None:
    catalog = default_team_personas()
    assert {profile.role: profile.display_name for profile in catalog.profiles} == {
        MemberRole.PLANNER: "白金",
        MemberRole.IMPLEMENTER: "月见",
        MemberRole.REVIEWER: "鲸鲸",
    }
    assert catalog.for_role(MemberRole.PLANNER).mention_patterns == (
        "@白金", "@codex", "@platinum"
    )
    assert any("CompletionGuard" in principle for principle in catalog.team_principles)
    with pytest.raises(ValueError, match="no persona"):
        catalog.for_role(MemberRole.VERIFIER)


def test_chat_personalities_are_distinct_without_changing_role_boundaries() -> None:
    catalog = default_team_personas()
    planner = catalog.for_role(MemberRole.PLANNER)
    implementer = catalog.for_role(MemberRole.IMPLEMENTER)
    reviewer = catalog.for_role(MemberRole.REVIEWER)

    assert "大小姐" in planner.personality and "取舍" in planner.personality
    assert "海风" in implementer.personality and "追问" in implementer.personality
    assert "小鲸鱼" in reviewer.personality and "具体" in reviewer.personality
    assert "不写实现代码" in planner.restrictions
    assert "不修改验收标准" in implementer.restrictions
    assert "没有证据不批准" in reviewer.restrictions


def test_profile_rejects_non_agent_role_bad_mentions_and_permission_fields() -> None:
    values = default_team_personas().for_role(MemberRole.PLANNER).model_dump(mode="json")
    with pytest.raises(ValidationError, match="Agent roles"):
        PersonaProfile.model_validate({**values, "role": "verifier"})
    with pytest.raises(ValidationError, match="mention patterns"):
        PersonaProfile.model_validate({**values, "mention_patterns": ["@bad mention"]})
    with pytest.raises(ValidationError, match="unique"):
        PersonaProfile.model_validate({**values, "mention_patterns": ["@Codex", "@codex"]})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PersonaProfile.model_validate({**values, "permission_mode": "workspace_write"})
    with pytest.raises(ValidationError, match="restrictions must not be blank"):
        PersonaProfile.model_validate({**values, "restrictions": [" "]})


def test_catalog_requires_all_roles_unique_names_and_mentions() -> None:
    catalog = default_team_personas()
    planner, implementer, reviewer = catalog.profiles
    with pytest.raises(ValidationError, match="one persona"):
        TeamPersonaCatalog(
            profiles=(planner, planner, reviewer),
            team_principles=catalog.team_principles,
        )

    duplicate_name = PersonaProfile.model_validate({
        **implementer.model_dump(mode="json"), "display_name": planner.display_name,
    })
    with pytest.raises(ValidationError, match="globally unique"):
        TeamPersonaCatalog(
            profiles=(planner, duplicate_name, reviewer),
            team_principles=catalog.team_principles,
        )

    system_name = PersonaProfile.model_validate({
        **implementer.model_dump(mode="json"), "display_name": "Verifier",
    })
    with pytest.raises(ValidationError, match="system room members"):
        TeamPersonaCatalog(
            profiles=(planner, system_name, reviewer),
            team_principles=catalog.team_principles,
        )

    duplicate_mention = PersonaProfile.model_validate({
        **reviewer.model_dump(mode="json"), "mention_patterns": ["@Whale", "@codex"],
    })
    with pytest.raises(ValidationError, match="globally unique"):
        TeamPersonaCatalog(
            profiles=(planner, implementer, duplicate_mention),
            team_principles=catalog.team_principles,
        )
    with pytest.raises(ValidationError, match="principles must not be blank"):
        TeamPersonaCatalog(profiles=catalog.profiles, team_principles=(" ",))
