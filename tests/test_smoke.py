"""Dependency-light smoke test.

Asserts the canonical constants from the specs exist and hold their pinned
values. Importing ``yalp.config`` must NOT require anthropic / opencv / numpy —
config is intentionally stdlib-only (plus optional dotenv).
"""

from yalp import config


def test_model_tiers():
    assert config.MODEL_FAST == "claude-haiku-4-5"
    assert config.MODEL_MID == "claude-sonnet-4-6"
    assert config.MODEL_BIG == "claude-opus-4-8"


def test_thresholds():
    assert config.GOOD_LIGHT_LUX == 200
    assert config.GATE_H_GO_HZ == 3
    assert config.TICK_BUDGET_MS == 33


def test_ipc_endpoint():
    assert config.IPC_HOST == "127.0.0.1"
    assert isinstance(config.IPC_PORT, int)


def test_config_dataclass_defaults_match_constants():
    cfg = config.Config()
    assert cfg.model_fast == config.MODEL_FAST
    assert cfg.gate_h_go_hz == config.GATE_H_GO_HZ
    assert cfg.tick_budget_ms == config.TICK_BUDGET_MS


def test_require_api_key_is_friendly_when_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.get_api_key() is None
    try:
        config.require_api_key()
    except RuntimeError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
        assert "SETUP.md" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("require_api_key() should raise when the key is unset")
