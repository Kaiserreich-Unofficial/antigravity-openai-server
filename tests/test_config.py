from __future__ import annotations

from app.config import Settings, resolve_model


def make_settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_display_name_passthrough_case_insensitive():
    s = make_settings()
    assert resolve_model("Gemini 4 Neon (Low)", s) == "Gemini 4 Neon (Low)"
    assert resolve_model("gemini 4 neon (low)", s) == "Gemini 4 Neon (Low)"
    assert resolve_model("claude sonnet 5.5 (medium)", s) == "Claude Sonnet 5.5 (Medium)"


def test_slug_with_effort_param():
    s = make_settings()
    assert resolve_model("gemini-3.8-flash", s) == "Gemini 3.8 Flash (Medium)"
    assert resolve_model("gemini-3.8-flash", s, effort="high") == "Gemini 3.8 Flash (High)"
    assert resolve_model("gemini-3.8-flash", s, effort="low") == "Gemini 3.8 Flash (Low)"
    assert resolve_model("gemini-4-neon", s, effort="xhigh") == "Gemini 4 Neon (XHigh)"


def test_slug_effort_suffix_overrides():
    s = make_settings()
    assert resolve_model("gemini-3.8-flash-low", s, effort="high") == "Gemini 3.8 Flash (Low)"
    assert resolve_model("gemini-4-hydrogen-xhigh", s) == "Gemini 4 Hydrogen (XHigh)"
    assert resolve_model("gemini-4-neon-high", s) == "Gemini 4 Neon (High)"


def test_family_without_effort_prefers_medium():
    s = make_settings()
    assert resolve_model("gemini-4-carbon", s) == "Gemini 4 Carbon (Medium)"
    assert resolve_model("gemini-4-argon", s, effort="high") == "Gemini 4 Argon (High)"


def test_single_variant_families_ignore_effort():
    s = make_settings()
    assert resolve_model("orchestrator", s, effort="low") == "Orchestrator (Carbon)"
    assert resolve_model("gpt-oss", s) == "GPT-OSS 120B (Medium)"
    assert resolve_model("opus", s) == "Claude Opus 5.5 (Medium)"
    assert resolve_model("sonnet", s, effort="low") == "Claude Sonnet 5.5 (Low)"
    # Clients that send levels above `high` land on the top variant.
    assert resolve_model("claude-sonnet-5.5", s, effort="max") == "Claude Sonnet 5.5 (High)"


def test_unknown_falls_back_to_default():
    s = make_settings(default_model="Gemini 4 Neon (High)")
    assert resolve_model("gpt-4o", s) == "Gemini 4 Neon (High)"
    assert resolve_model(None, s) == "Gemini 4 Neon (High)"
    assert resolve_model("", s) == "Gemini 4 Neon (High)"


def test_loopback_detection():
    assert make_settings(host="127.0.0.1").is_loopback_host()
    assert make_settings(host="localhost").is_loopback_host()
    assert not make_settings(host="0.0.0.0").is_loopback_host()


def test_workdir_guard(tmp_path):
    s = make_settings(default_workdir=tmp_path / "ws")
    assert s.resolve_workdir(None) == tmp_path / "ws"
    inside = s.resolve_workdir(str(tmp_path / "ws" / "sub"))
    assert inside.name == "sub"
    try:
        s.resolve_workdir(str(tmp_path / "elsewhere"))
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for out-of-root workdir")
