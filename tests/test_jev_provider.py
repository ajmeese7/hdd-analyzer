from hdd_analyzer.jev_provider import (
    NATIVE_MODEL,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    resolve_provider,
    rewrite_path,
)


def test_resolve_provider_native_key(monkeypatch):
    monkeypatch.delenv("JEV_PROVIDER", raising=False)
    config = resolve_provider("ts-live-abc123")
    assert config.name == "typesafe"
    assert config.model == NATIVE_MODEL
    assert config.client_kwargs == {}


def test_resolve_provider_openrouter_key(monkeypatch):
    monkeypatch.delenv("JEV_PROVIDER", raising=False)
    config = resolve_provider("sk-or-abc123")
    assert config.name == "openrouter"
    assert config.model == OPENROUTER_MODEL
    assert config.client_kwargs["base_url"] == OPENROUTER_BASE_URL
    assert "transport" in config.client_kwargs


def test_resolve_provider_missing_key_defaults_native(monkeypatch):
    monkeypatch.delenv("JEV_PROVIDER", raising=False)
    config = resolve_provider(None)
    assert config.name == "typesafe"


def test_env_override_forces_openrouter(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "openrouter")
    config = resolve_provider("ts-live-abc123")
    assert config.name == "openrouter"


def test_env_override_forces_typesafe(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "typesafe")
    config = resolve_provider("sk-or-abc123")
    assert config.name == "typesafe"


def test_env_override_case_insensitive(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER", "OpenRouter")
    config = resolve_provider("ts-live-abc123")
    assert config.name == "openrouter"


def test_rewrite_path_maps_systemone_to_decisions():
    assert rewrite_path("/v1/systemone") == "/api/alpha/decisions"


def test_rewrite_path_leaves_other_paths_unchanged():
    assert rewrite_path("/v1/health") == "/v1/health"
