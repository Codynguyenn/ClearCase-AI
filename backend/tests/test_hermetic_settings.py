"""The test suite must never reach a paid model, whatever the developer's .env says.

This was found the hard way: with ``CLEARCASE_ASSISTANT_MODE=live`` and a real key in .env, a demo-assistant
test silently called the provider. The shared test settings now ignore .env and the environment's assistant
settings, and this test keeps them that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_the_shared_test_settings_are_demo_mode_with_no_key(test_settings: Settings):
    assert test_settings.assistant_mode == "demo"
    assert not test_settings.llm_api_key and not test_settings.llm_provider


def test_the_shared_test_settings_ignore_a_live_mode_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Same construction the fixture uses, evaluated under a hostile environment and a hostile .env."""
    env = tmp_path / ".env"
    env.write_text("CLEARCASE_ASSISTANT_MODE=live\nCLEARCASE_LLM_PROVIDER=anthropic\nCLEARCASE_LLM_API_KEY=not-a-real-key\n")
    monkeypatch.chdir(tmp_path)  # Settings also looks for ./.env
    leaked = Settings()
    assert leaked.assistant_mode == "live" and leaked.llm_api_key  # the hazard is real: a plain Settings() picks it up

    from tests.conftest import test_settings as fixture  # the fixture function itself

    built = fixture.__wrapped__() if hasattr(fixture, "__wrapped__") else None
    assert built is not None, "could not reach the fixture function"
    assert built.assistant_mode == "demo" and not built.llm_api_key and not built.llm_provider
