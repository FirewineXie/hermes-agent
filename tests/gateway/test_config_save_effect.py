"""Saved configuration reports the effect guaranteed by Hermes session policy."""

from hermes_cli.web_config_effect import config_save_effect


def test_followed_model_change_applies_on_next_turn():
    before = {"model": {"default": "old"}}
    after = {"model": {"default": "new"}}

    assert config_save_effect(before, after) == {"status": "next_turn"}


def test_gateway_change_requires_restart_even_when_agent_field_follows():
    before = {"agent": {"reasoning_effort": "low"}, "gateway": {"multiplex_profiles": False}}
    after = {"agent": {"reasoning_effort": "high"}, "gateway": {"multiplex_profiles": True}}

    assert config_save_effect(before, after) == {"status": "gateway_restart_required"}
