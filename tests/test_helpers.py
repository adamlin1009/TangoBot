from __future__ import annotations

import importlib

import pytest


def _load_app_module():
    try:
        return importlib.import_module("app")
    except ModuleNotFoundError:
        pytest.skip("app module is not present yet")


def _get_helper(module, *names):
    for name in names:
        helper = getattr(module, name, None)
        if callable(helper):
            return helper
    pytest.fail(f"None of these helpers exist on app: {', '.join(names)}")


def test_parse_help_command():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("help")

    if isinstance(parsed, dict):
        assert parsed.get("action", parsed.get("kind")) == "help"
        assert parsed.get("filename") in (None, "")
        assert parsed.get("prompt") in (None, "")
    elif hasattr(parsed, "kind"):
        assert parsed.kind == "help"
        assert getattr(parsed, "filename", None) in (None, "")
        assert getattr(parsed, "prompt", None) in (None, "")
    else:
        assert parsed[0] == "help"
        assert len(parsed) >= 1


def test_parse_generate_command():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("generate market-map.html enterprise AI landscape")

    if isinstance(parsed, dict):
        assert parsed.get("action", parsed.get("kind")) == "generate"
        assert parsed.get("filename") == "market-map.html"
        assert parsed.get("prompt") == "enterprise AI landscape"
    elif hasattr(parsed, "kind"):
        assert parsed.kind == "generate"
        assert parsed.filename == "market-map.html"
        assert parsed.prompt == "enterprise AI landscape"
    else:
        assert parsed[0] == "generate"
        assert parsed[1] == "market-map.html"
        assert parsed[2] == "enterprise AI landscape"


def test_parse_plain_language_generates_with_auto_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("make me a dashboard for enterprise AI startups")

    assert parsed.kind == "generate"
    assert parsed.filename == "make-me-a-dashboard-for-enterprise.html"
    assert parsed.prompt == "make me a dashboard for enterprise AI startups"


def test_parse_plain_language_uses_mentioned_html_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("create market-map.html for enterprise AI startups")

    assert parsed.kind == "generate"
    assert parsed.filename == "market-map.html"
    assert parsed.prompt == "create for enterprise AI startups"


def test_prefix_and_slug_filename_helpers():
    app = _load_app_module()
    build_filename = _get_helper(
        app,
        "build_storage_filename",
        "prefix_filename",
        "make_storage_filename",
    )

    prefixed = build_filename("U12345", "market map.html")

    assert prefixed.startswith("U12345-")
    assert prefixed.endswith(".html")
    assert "/" not in prefixed
    assert "\\" not in prefixed
    assert "market-map" in prefixed


def test_prompt_to_filename_slugging_helper():
    app = _load_app_module()
    build_filename = _get_helper(
        app,
        "filename_from_prompt",
        "slugify_prompt_filename",
        "derive_filename_from_prompt",
    )

    filename = build_filename(
        "enterprise AI landscape with columns for category, company, funding, and stage"
    )

    assert filename.endswith(".html")
    assert "enterprise-ai-landscape" in filename
    assert "/" not in filename
    assert "\\" not in filename


def test_file_share_dm_events_are_not_ignored():
    app = _load_app_module()
    should_ignore = _get_helper(app, "should_ignore_message_event")

    assert should_ignore({"channel_type": "im", "subtype": "file_share", "user": "U12345"}) is False
    assert should_ignore({"channel_type": "im", "subtype": "message_changed", "user": "U12345"}) is True
    assert should_ignore({"channel_type": "channel", "user": "U12345"}) is True
