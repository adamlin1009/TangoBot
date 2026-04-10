from __future__ import annotations

import importlib
from types import SimpleNamespace

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


def _test_config(app):
    return app.AppConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        anthropic_api_key="sk-ant-test",
        anthropic_model="claude-sonnet-4-6",
        sites_dir=app.Path("/tmp/sites"),
        tailscale_bin="tailscale",
        tailscale_base_url="https://example.ts.net",
        web_search_enabled=True,
        web_search_max_uses=3,
    )


class _FakeAnthropic:
    def __init__(self, response_text):
        self.messages = self
        self.response_text = response_text
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[{"type": "text", "text": self.response_text}],
            stop_reason="end_turn",
        )


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


@pytest.mark.parametrize("message", ["guide", "usage", "instructions", "what can you do?"])
def test_parse_help_aliases(message):
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command(message)

    assert parsed.kind == "help"


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


def test_parse_generate_without_prompt_infers_from_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("generate x.html")

    assert parsed.kind == "generate"
    assert parsed.filename == "x.html"
    assert parsed.prompt == "Create a polished single-page web page for: x."


def test_parse_natural_language_filename_only_infers_from_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("create market-map.html")

    assert parsed.kind == "generate"
    assert parsed.filename == "market-map.html"
    assert parsed.prompt == "Create a polished single-page web page for: market map."


def test_parse_plain_language_routes_for_claude_intent():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("make me a dashboard for enterprise AI startups")

    assert parsed.kind == "route"
    assert parsed.filename is None
    assert parsed.prompt == "make me a dashboard for enterprise AI startups"


def test_route_message_intent_can_generate_from_plain_language():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"generate","filename":null,"prompt":"make me a marketplace map for enterprise ai"}'
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        "make me a marketplace map for enterprise ai",
    )

    assert parsed.kind == "generate"
    assert parsed.filename == "marketplace-map-enterprise-ai.html"
    assert parsed.prompt == "make me a marketplace map for enterprise ai"


def test_route_message_intent_can_chat_from_plain_language():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"chat","filename":null,"prompt":"What can you help me with?"}'
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        "What can you help me with?",
    )

    assert parsed.kind == "chat"
    assert parsed.filename is None
    assert parsed.prompt == "What can you help me with?"


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
    assert parsed.prompt == "enterprise AI startups"


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


def test_jsx_filename_helpers_preserve_source_and_page_names():
    app = _load_app_module()
    build_jsx_source_filename = _get_helper(app, "build_jsx_source_filename")
    build_jsx_page_filename = _get_helper(app, "build_jsx_page_filename")

    assert build_jsx_source_filename("U12345", "Market Map.jsx") == "U12345-market-map.jsx"
    assert build_jsx_page_filename("U12345", "Market Map.jsx") == "U12345-market-map.html"


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


def test_source_upload_file_types_are_supported():
    app = _load_app_module()
    is_supported_upload = _get_helper(app, "is_supported_upload")
    is_supported_source_upload = _get_helper(app, "is_supported_source_upload")

    assert is_supported_upload({"name": "page.html"}) is True
    assert is_supported_upload({"name": "component.jsx"}) is True
    assert is_supported_upload({"name": "companies.csv"}) is False
    assert is_supported_source_upload({"name": "companies.csv"}) is True
    assert is_supported_source_upload({"name": "notes.md"}) is True
    assert is_supported_source_upload({"name": "page.html"}) is False
    assert is_supported_source_upload({"name": "component.jsx"}) is False


@pytest.mark.parametrize(
    ("jsx_source", "component_name"),
    [
        ("function App() { return <main>Hello</main>; }", "App"),
        ("const App = () => <main>Hello</main>;", "App"),
        ("const App = () => <main>Hello</main>; export default App;", "App"),
        ("export default function App() { return <main>Hello</main>; }", "App"),
        ("function MarketMap() { return <main>Hello</main>; }\nexport default MarketMap;", "MarketMap"),
    ],
)
def test_jsx_component_detection_supports_common_patterns(jsx_source, component_name):
    app = _load_app_module()
    validate_jsx_source = _get_helper(app, "validate_jsx_source")
    wrap_jsx_as_html = _get_helper(app, "wrap_jsx_as_html")

    assert validate_jsx_source(jsx_source) == component_name
    html = wrap_jsx_as_html(jsx_source, "Market Map")

    assert "https://unpkg.com/react@18/umd/react.production.min.js" in html
    assert "https://unpkg.com/react-dom@18/umd/react-dom.production.min.js" in html
    assert "https://unpkg.com/@babel/standalone/babel.min.js" in html
    assert '<div id="root"></div>' in html
    assert f"tangobotRoot.render(<{component_name} />);" in html
    assert "export default" not in html


def test_jsx_upload_rejects_imports():
    app = _load_app_module()
    validate_jsx_source = _get_helper(app, "validate_jsx_source")

    with pytest.raises(ValueError, match="import"):
        validate_jsx_source('import React from "react";\nfunction App() { return <main />; }')


def test_jsx_upload_requires_component():
    app = _load_app_module()
    validate_jsx_source = _get_helper(app, "validate_jsx_source")

    with pytest.raises(ValueError, match="Could not find a React component"):
        validate_jsx_source("const message = 'hello';")


def test_source_only_upload_uses_source_filename():
    app = _load_app_module()
    filename_from_source_files = _get_helper(app, "filename_from_source_files")
    prompt_from_source_filenames = _get_helper(app, "prompt_from_source_filenames")

    files = [{"name": "companies.csv"}]

    assert filename_from_source_files(files) == "companies.html"
    assert prompt_from_source_filenames(files) == (
        "Create a polished single-page web page from the attached source material: companies."
    )


def test_source_upload_with_text_forces_generation():
    app = _load_app_module()
    command_for_source_generation = _get_helper(app, "command_for_source_generation")

    command = command_for_source_generation(
        "make me a marketplace map for enterprise ai",
        [{"name": "companies.csv"}],
    )

    assert command.kind == "generate"
    assert command.filename == "marketplace-map-enterprise-ai.html"
    assert command.prompt == "make me a marketplace map for enterprise ai"


def test_build_prompt_with_source_material():
    app = _load_app_module()
    build_prompt_with_sources = _get_helper(app, "build_prompt_with_sources")

    prompt = build_prompt_with_sources(
        "make me a marketplace map for enterprise ai",
        [{"name": "companies.csv", "content": "Company,Category\nOpenAI,Foundation models"}],
    )

    assert "User request:" in prompt
    assert "make me a marketplace map for enterprise ai" in prompt
    assert "--- Source file: companies.csv ---" in prompt
    assert "OpenAI,Foundation models" in prompt


def test_web_search_tools_can_be_enabled():
    app = _load_app_module()
    web_search_tools = _get_helper(app, "web_search_tools")
    config = _test_config(app)

    assert web_search_tools(config) == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
        }
    ]


def test_sources_section_is_appended_before_body_close():
    app = _load_app_module()
    append_sources_section = _get_helper(app, "append_sources_section")

    html = append_sources_section(
        "<html><body><h1>Market Map</h1></body></html>",
        [{"url": "https://example.com?a=1&b=2", "title": "Example & Co"}],
    )

    assert "https://example.com?a=1&amp;b=2" in html
    assert "Example &amp; Co" in html
    assert html.index("<section") < html.index("</body>")


def test_extracts_text_and_sources_from_dict_blocks():
    app = _load_app_module()
    extract_text_content = _get_helper(app, "extract_text_content")
    extract_cited_sources = _get_helper(app, "extract_cited_sources")

    blocks = [
        {
            "type": "text",
            "text": "<html>",
            "citations": [
                {
                    "type": "web_search_result_location",
                    "url": "https://example.com/source",
                    "title": "Example Source",
                }
            ],
        },
        {"type": "text", "text": "</html>"},
    ]

    assert extract_text_content(blocks) == "<html>\n</html>"
    assert extract_cited_sources(blocks) == [
        {"url": "https://example.com/source", "title": "Example Source"}
    ]
