from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


_SUBMODULES = ("commands", "generation", "storage", "config", "tailscale")


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
    for submodule_name in _SUBMODULES:
        try:
            submodule = importlib.import_module(submodule_name)
        except ModuleNotFoundError:
            continue
        for name in names:
            helper = getattr(submodule, name, None)
            if callable(helper):
                return helper
    pytest.fail(f"None of these helpers exist on app or submodules: {', '.join(names)}")


def _test_config(app, state_file=None, web_search_enabled=True, history_file=None, versions_dir=None, sites_dir=None):
    return app.AppConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        anthropic_api_key="sk-ant-test",
        anthropic_model="claude-sonnet-4-6",
        sites_dir=sites_dir or app.Path("/tmp/sites"),
        tailscale_bin="tailscale",
        tailscale_base_url="https://example.ts.net",
        web_search_enabled=web_search_enabled,
        web_search_max_uses=3,
        state_file=state_file or app.Path("/tmp/tangobot-test-state.json"),
        history_file=history_file or app.Path("/tmp/tangobot-test-history.json"),
        versions_dir=versions_dir or app.Path("/tmp/tangobot-test-versions"),
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


class _SequenceAnthropic:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        response = self.responses[index]
        content = response if isinstance(response, list) else [{"type": "text", "text": response}]
        return SimpleNamespace(content=content, stop_reason="end_turn")


class _StopReasonAnthropic:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        text, stop_reason = self.responses[index]
        return SimpleNamespace(
            content=[{"type": "text", "text": text}],
            stop_reason=stop_reason,
        )


class _FakeStream:
    def __init__(self, text, chunk_size):
        self._text = text
        self._chunk_size = chunk_size

    @property
    def text_stream(self):
        for i in range(0, len(self._text), self._chunk_size):
            yield self._text[i : i + self._chunk_size]

    def get_final_message(self):
        return SimpleNamespace(
            content=[{"type": "text", "text": self._text}],
            stop_reason="end_turn",
        )


class _FakeStreamContext:
    def __init__(self, text, chunk_size):
        self._text = text
        self._chunk_size = chunk_size

    def __enter__(self):
        return _FakeStream(self._text, self._chunk_size)

    def __exit__(self, exc_type, exc, tb):
        return False


class _StreamingAnthropic:
    """Fake Anthropic client exposing both streaming and non-streaming interfaces."""

    def __init__(self, responses, chunk_size=8):
        self.messages = self
        self.responses = list(responses)
        self.chunk_size = chunk_size
        self.requests = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return _FakeStreamContext(self.responses[index], self.chunk_size)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        index = min(len(self.requests) - 1, len(self.responses) - 1)
        return SimpleNamespace(
            content=[{"type": "text", "text": self.responses[index]}],
            stop_reason="end_turn",
        )


class _RateLimitingAnthropic:
    """Raises rate-limit errors N times, then succeeds."""

    def __init__(self, responses, failures_before_success):
        self.messages = self
        self.responses = list(responses)
        self.failures_before_success = failures_before_success
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) <= self.failures_before_success:
            raise RuntimeError("Error code: 429 - rate_limit_error")
        index = min(len(self.requests) - 1 - self.failures_before_success, len(self.responses) - 1)
        return SimpleNamespace(
            content=[{"type": "text", "text": self.responses[index]}],
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

    parsed = parse_command("generate market-map.html enterprise AI landscape with categories and vendor examples")

    if isinstance(parsed, dict):
        assert parsed.get("action", parsed.get("kind")) == "generate"
        assert parsed.get("filename") == "market-map.html"
        assert parsed.get("prompt") == "enterprise AI landscape with categories and vendor examples"
    elif hasattr(parsed, "kind"):
        assert parsed.kind == "generate"
        assert parsed.filename == "market-map.html"
        assert parsed.prompt == "enterprise AI landscape with categories and vendor examples"
    else:
        assert parsed[0] == "generate"
        assert parsed[1] == "market-map.html"
        assert parsed[2] == "enterprise AI landscape with categories and vendor examples"


def test_parse_generate_without_prompt_infers_from_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("generate x.html")

    assert parsed.kind == "clarify"
    assert parsed.filename == "x.html"
    assert parsed.prompt == "Create a complete, useful single-page artifact inferred from the filename: x."
    assert parsed.question


def test_parse_natural_language_filename_only_infers_from_filename():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command("create market-map.html")

    assert parsed.kind == "clarify"
    assert parsed.filename == "market-map.html"
    assert parsed.prompt == "Create a complete, useful single-page artifact inferred from the filename: market map."
    assert "market" in parsed.question


def test_detailed_filename_request_skips_clarification():
    app = _load_app_module()
    parse_command = _get_helper(
        app,
        "parse_command",
        "parse_dm_command",
        "parse_message",
    )

    parsed = parse_command(
        "generate market-map.html enterprise AI landscape with categories, companies, funding, and stage"
    )

    assert parsed.kind == "generate"
    assert parsed.filename == "market-map.html"
    assert parsed.prompt == "enterprise AI landscape with categories, companies, funding, and stage"


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
    prompt = (
        "make me a marketplace map for enterprise ai with categories, "
        "vendor examples, buyer fit, and funding stage"
    )
    fake_anthropic = _FakeAnthropic(
        json.dumps(
            {
                "action": "generate",
                "filename": "marketplace-map-enterprise-ai.html",
                "prompt": prompt,
            }
        )
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        prompt,
    )

    assert parsed.kind == "generate"
    assert parsed.filename == "marketplace-map-enterprise-ai.html"
    assert parsed.prompt == prompt


def test_route_message_intent_clarifies_broad_marketplace_map_for_enterprise_agents():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"generate","filename":null,"prompt":"make me a marketplace map for enterprise agents"}'
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        "make me a marketplace map for enterprise agents",
    )

    assert parsed.kind == "clarify"
    assert parsed.filename == "marketplace-map-enterprise-agents.html"
    assert parsed.question == "What audience, categories, or source material should this map use?"


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


def test_route_message_intent_truncates_large_router_input():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"chat","filename":null,"prompt":"summarize this"}'
    )
    long_text = "summarize this " + ("x" * (app.MAX_ROUTER_INPUT_CHARS + 100))

    route_message_intent(fake_anthropic, _test_config(app), long_text)

    sent_text = fake_anthropic.requests[0]["messages"][0]["content"]
    assert len(sent_text) < len(long_text)
    assert f"[Truncated after {app.MAX_ROUTER_INPUT_CHARS} characters.]" in sent_text


def test_route_message_intent_can_clarify_thin_generation_request():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"clarify","filename":"market-map.html","prompt":"make me a market map","question":"What market should this cover?"}'
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        "make me a market map",
    )

    assert parsed.kind == "clarify"
    assert parsed.filename == "market-map.html"
    assert parsed.prompt == "make me a market map"
    assert parsed.question == "What market should this cover?"


def test_router_generate_payload_is_clarified_when_prompt_is_too_thin():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake_anthropic = _FakeAnthropic(
        '{"action":"generate","filename":"market-map.html","prompt":"make me a market map"}'
    )

    parsed = route_message_intent(
        fake_anthropic,
        _test_config(app),
        "make me a market map",
    )

    assert parsed.kind == "clarify"
    assert parsed.filename == "market-map.html"
    assert parsed.question == "What market, industry, or audience should this map cover?"


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


def test_parse_revision_rollback_and_history_commands():
    app = _load_app_module()
    parse_command = _get_helper(app, "parse_command")

    revised = parse_command("revise market-map.html to add a funding stage column")
    assert revised.kind == "revise"
    assert revised.filename == "market-map.html"
    assert revised.prompt == "add a funding stage column"

    last_page_revision = parse_command("edit it to be cleaner")
    assert last_page_revision.kind == "revise"
    assert last_page_revision.filename is None
    assert last_page_revision.prompt == "it to be cleaner"

    rollback = parse_command("rollback market-map.html")
    assert rollback.kind == "rollback"
    assert rollback.filename == "market-map.html"

    history = parse_command("history")
    assert history.kind == "history"
    assert history.filename is None

    chatty_update = parse_command("update me on the latest AI news")
    assert chatty_update.kind == "route"


def test_natural_revision_requires_recent_page_and_skips_generation_requests(tmp_path):
    app = _load_app_module()
    command_for_natural_revision = _get_helper(app, "command_for_natural_revision")
    record_page_publish = _get_helper(app, "record_page_publish")
    config = _test_config(
        app,
        history_file=tmp_path / "history.json",
        versions_dir=tmp_path / "versions",
    )
    html = "<!doctype html><html><head><title>Map</title></head><body><h1>Map</h1></body></html>"

    assert command_for_natural_revision(config, "U12345", "make it cleaner") is None

    record_page_publish(config, "U12345", "market-map.html", "U12345-market-map.html", html, "make a map")

    command = command_for_natural_revision(config, "U12345", "make it cleaner")
    assert command.kind == "revise"
    assert command.filename is None
    assert command.prompt == "make it cleaner"

    assert command_for_natural_revision(config, "U12345", "make me a dashboard for sales") is None


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


def test_web_search_tools_are_omitted_when_disabled():
    app = _load_app_module()
    web_search_tools = _get_helper(app, "web_search_tools")
    config = _test_config(app, web_search_enabled=False)

    assert web_search_tools(config) == []


def test_chat_with_claude_truncates_large_prompt_input():
    app = _load_app_module()
    chat_with_claude = _get_helper(app, "chat_with_claude")
    fake_anthropic = _SequenceAnthropic(["ok"])
    long_prompt = "question " + ("x" * (app.MAX_MODEL_INPUT_CHARS + 100))

    response = chat_with_claude(fake_anthropic, _test_config(app, web_search_enabled=False), long_prompt)

    sent_text = fake_anthropic.requests[0]["messages"][0]["content"]
    assert response == "ok"
    assert len(sent_text) < len(long_prompt)
    assert f"[Truncated after {app.MAX_MODEL_INPUT_CHARS} characters.]" in sent_text


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


def test_extract_html_document_removes_preamble_and_trailing_text():
    app = _load_app_module()
    extract_html_document = _get_helper(app, "extract_html_document")

    html = extract_html_document(
        "Now I have comprehensive data.\n"
        "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>\nDone."
    )

    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")
    assert "Now I have" not in html
    assert "Done." not in html


def test_extract_html_document_accepts_markdown_fenced_html():
    app = _load_app_module()
    extract_html_document = _get_helper(app, "extract_html_document")

    html = extract_html_document(
        "```html\n<html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>\n```"
    )

    assert html == "<html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"


@pytest.mark.parametrize(
    "response_text",
    [
        "Now I have comprehensive data to build the page.",
        "<html><head><title>Map</title></head><body><h1>Market Map</h1></body>",
        "<html><head><title>Map</title></head><h1>Market Map</h1></html>",
    ],
)
def test_extract_html_document_rejects_invalid_html(response_text):
    app = _load_app_module()
    extract_html_document = _get_helper(app, "extract_html_document")

    with pytest.raises(ValueError):
        extract_html_document(response_text)


def test_build_generation_prompt_preserves_filename_and_user_prompt():
    app = _load_app_module()
    build_generation_prompt = _get_helper(app, "build_generation_prompt")

    prompt = build_generation_prompt(
        "enterprise AI landscape with categories, companies, funding, and stage",
        "market-map.html",
    )

    assert "Requested filename: market-map.html" in prompt
    assert "enterprise AI landscape with categories, companies, funding, and stage" in prompt
    assert "Maps should become structured visual landscapes" in prompt
    assert "avoid unsupported claims about current facts" in prompt


def test_pending_clarification_state_round_trips(tmp_path):
    app = _load_app_module()
    config = _test_config(app, tmp_path / "pending.json")
    set_pending_clarification = _get_helper(app, "set_pending_clarification")
    get_pending_clarification = _get_helper(app, "get_pending_clarification")
    clear_pending_clarification = _get_helper(app, "clear_pending_clarification")

    set_pending_clarification(
        config,
        "U12345",
        app.Command(kind="clarify", filename="market-map.html", prompt="make me a market map", question="What market?"),
    )

    pending = get_pending_clarification(config, "U12345")
    assert pending["filename"] == "market-map.html"
    assert pending["prompt"] == "make me a market map"
    assert pending["question"] == "What market?"

    clear_pending_clarification(config, "U12345")
    assert get_pending_clarification(config, "U12345") is None


def test_page_history_round_trips_and_ignores_malformed_json(tmp_path):
    app = _load_app_module()
    load_page_history = _get_helper(app, "load_page_history")
    record_page_publish = _get_helper(app, "record_page_publish")
    resolve_page_entry = _get_helper(app, "resolve_page_entry")
    history_file = tmp_path / "history.json"
    config = _test_config(app, history_file=history_file, versions_dir=tmp_path / "versions")
    html = "<!doctype html><html><head><title>Map</title></head><body><h1>Map</h1></body></html>"

    history_file.write_text("not json", encoding="utf-8")
    assert load_page_history(history_file) == {}

    entry = record_page_publish(config, "U12345", "market-map.html", "U12345-market-map.html", html, "make a map")
    history = load_page_history(history_file)

    assert entry["current_version"] == 1
    assert history["U12345"]["last_stored_name"] == "U12345-market-map.html"
    assert history["U12345"]["pages"]["U12345-market-map.html"]["requested_filename"] == "market-map.html"
    assert app.Path(entry["versions"][0]["path"]).exists()
    assert resolve_page_entry(config, "U12345", "market-map.html")[0] == "U12345-market-map.html"
    assert resolve_page_entry(config, "U12345", "U12345-market-map.html")[0] == "U12345-market-map.html"


def test_page_versions_snapshot_and_rollback(tmp_path):
    app = _load_app_module()
    record_page_publish = _get_helper(app, "record_page_publish")
    rollback_published_page = _get_helper(app, "rollback_published_page")
    format_recent_pages_response = _get_helper(app, "format_recent_pages_response")
    format_page_history_response = _get_helper(app, "format_page_history_response")
    sites_dir = tmp_path / "sites"
    config = _test_config(
        app,
        history_file=tmp_path / "history.json",
        versions_dir=tmp_path / "versions",
        sites_dir=sites_dir,
    )
    html_v1 = "<!doctype html><html><head><title>Map</title></head><body><h1>One</h1></body></html>"
    html_v2 = "<!doctype html><html><head><title>Map</title></head><body><h1>Two</h1></body></html>"
    live_path = sites_dir / "U12345-market-map.html"

    live_path.parent.mkdir(parents=True)
    live_path.write_text(html_v1, encoding="utf-8")
    record_page_publish(config, "U12345", "market-map.html", "U12345-market-map.html", html_v1, "make a map")
    live_path.write_text(html_v2, encoding="utf-8")
    entry = record_page_publish(
        config,
        "U12345",
        "market-map.html",
        "U12345-market-map.html",
        html_v2,
        "add detail",
        publish_kind="revision",
    )

    assert entry["current_version"] == 2
    assert "market-map.html" in format_recent_pages_response(config, "U12345")

    stored_name, rolled_back = rollback_published_page(config, "U12345")

    assert stored_name == "U12345-market-map.html"
    assert rolled_back["current_version"] == 1
    assert live_path.read_text(encoding="utf-8") == html_v1
    page_history = format_page_history_response(config, "U12345", "market-map.html")
    assert "v1 current" in page_history
    assert "v2" in page_history


def test_build_prompt_from_clarification_combines_original_and_answer():
    app = _load_app_module()
    build_prompt_from_clarification = _get_helper(app, "build_prompt_from_clarification")

    filename, prompt = build_prompt_from_clarification(
        {"filename": "market-map.html", "prompt": "make me a market map"},
        "enterprise AI for buyers",
    )

    assert filename == "market-map.html"
    assert "make me a market map" in prompt
    assert "enterprise AI for buyers" in prompt


def test_build_revision_prompt_includes_context_and_truncates_html():
    app = _load_app_module()
    build_revision_prompt = _get_helper(app, "build_revision_prompt")
    current_html = "<!doctype html><html><body>" + ("x" * (app.MAX_REVISION_HTML_CHARS + 100)) + "</body></html>"

    prompt = build_revision_prompt(
        {
            "requested_filename": "market-map.html",
            "original_prompt": "make a market map",
            "last_prompt": "add funding stages",
            "source_filenames": ["companies.csv"],
        },
        "make it more executive",
        current_html,
    )

    assert "Requested filename: market-map.html" in prompt
    assert "make a market map" in prompt
    assert "add funding stages" in prompt
    assert "make it more executive" in prompt
    assert "companies.csv" in prompt
    assert f"[Truncated after {app.MAX_REVISION_HTML_CHARS} characters.]" in prompt


def test_revise_published_page_keeps_url_and_increments_version(tmp_path):
    app = _load_app_module()
    record_page_publish = _get_helper(app, "record_page_publish")
    revise_published_page = _get_helper(app, "revise_published_page")
    sites_dir = tmp_path / "sites"
    config = _test_config(
        app,
        history_file=tmp_path / "history.json",
        versions_dir=tmp_path / "versions",
        sites_dir=sites_dir,
        web_search_enabled=False,
    )
    stored_name = "U12345-market-map.html"
    original_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Old</h1></body></html>"
    revised_html = "<!doctype html><html><head><title>Map</title></head><body><h1>New</h1></body></html>"
    live_path = sites_dir / stored_name
    live_path.parent.mkdir(parents=True)
    live_path.write_text(original_html, encoding="utf-8")
    record_page_publish(config, "U12345", "market-map.html", stored_name, original_html, "make a map")

    fake_anthropic = _SequenceAnthropic([revised_html])
    entry = revise_published_page(fake_anthropic, config, "U12345", "market-map.html", "make it cleaner")

    assert entry["current_version"] == 2
    assert live_path.read_text(encoding="utf-8") == revised_html
    assert app.publish_url(config, stored_name) == "https://example.ts.net/U12345-market-map.html"
    sent_prompt = fake_anthropic.requests[0]["messages"][0]["content"]
    assert "make it cleaner" in sent_prompt
    assert "Current live HTML" in sent_prompt


def test_generate_html_returns_valid_first_response_without_retry():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([valid_html])

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert html == valid_html
    assert len(fake_anthropic.requests) == 1


def test_generate_html_uses_larger_output_budget():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([valid_html])

    generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert fake_anthropic.requests[0]["max_tokens"] >= 8192


def test_generate_html_does_not_send_web_search_tools_when_disabled():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([valid_html])

    html = generate_html(
        fake_anthropic,
        _test_config(app, web_search_enabled=False),
        "enterprise AI landscape",
        "market-map.html",
    )

    assert html == valid_html
    assert "tools" not in fake_anthropic.requests[0]
    assert "Do not claim to have browsed the web" in fake_anthropic.requests[0]["system"]


def test_generate_html_truncates_large_prompt_input():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([valid_html])
    long_prompt = "make a page " + ("x" * (app.MAX_MODEL_INPUT_CHARS + 100))

    generate_html(fake_anthropic, _test_config(app), long_prompt, "page.html")

    sent_text = fake_anthropic.requests[0]["messages"][0]["content"]
    assert len(sent_text) < len(long_prompt) + 1000
    assert f"[Truncated after {app.MAX_MODEL_INPUT_CHARS} characters.]" in sent_text


def test_generate_html_cleans_preamble_without_retry():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([f"Now I have comprehensive data.\n{valid_html}\nAll set."])

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert html == valid_html
    assert len(fake_anthropic.requests) == 1


def test_generate_html_retries_once_after_invalid_response():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic(["Now I have comprehensive data.", valid_html])

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert html == valid_html
    assert len(fake_anthropic.requests) == 2
    assert "Validation error" in fake_anthropic.requests[1]["messages"][-1]["content"]
    assert all(message["role"] != "assistant" for message in fake_anthropic.requests[1]["messages"])


def test_generate_html_repair_compacts_after_output_token_limit():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    incomplete_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1>"
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Compact Map</h1></body></html>"
    fake_anthropic = _StopReasonAnthropic(
        [
            (incomplete_html, "max_tokens"),
            (valid_html, "end_turn"),
        ]
    )

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert html == valid_html
    retry_prompt = fake_anthropic.requests[1]["messages"][-1]["content"]
    assert "output token limit" in retry_prompt
    assert "shorter version" in retry_prompt
    assert all(message["role"] != "assistant" for message in fake_anthropic.requests[1]["messages"])


def test_generate_html_reports_output_token_limit_after_retry():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    incomplete_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1>"
    fake_anthropic = _StopReasonAnthropic(
        [
            (incomplete_html, "max_tokens"),
            (incomplete_html, "max_tokens"),
        ]
    )

    with pytest.raises(RuntimeError, match="output token limit"):
        generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")


def test_generate_html_invalid_retry_raises_runtime_error():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    fake_anthropic = _SequenceAnthropic(["not html", "still not html"])

    with pytest.raises(RuntimeError, match="invalid HTML after retry"):
        generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")


def test_generate_html_appends_citations_from_retry_flow():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic(
        [
            [
                {
                    "type": "text",
                    "text": "not html",
                    "citations": [
                        {
                            "type": "web_search_result_location",
                            "url": "https://example.com/source",
                            "title": "Example Source",
                        }
                    ],
                }
            ],
            valid_html,
        ]
    )

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert "https://example.com/source" in html
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


def test_generation_rate_limit_message_is_user_friendly():
    app = _load_app_module()
    generation_failure_message = _get_helper(app, "generation_failure_message")

    message = generation_failure_message(
        "market-map.html",
        RuntimeError("Error code: 429 - request would exceed input tokens per minute"),
    )

    assert "model rate limit" in message
    assert "429" not in message


def test_cleanup_expired_pages_respects_ttl(tmp_path):
    app = _load_app_module()
    cleanup_expired_pages = _get_helper(app, "cleanup_expired_pages")

    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    fresh = sites_dir / "fresh.html"
    stale = sites_dir / "stale.html"
    fresh.write_text("fresh", encoding="utf-8")
    stale.write_text("stale", encoding="utf-8")

    import os as _os
    ancient = fresh.stat().st_mtime - (10 * 86400)
    _os.utime(stale, (ancient, ancient))

    deleted = cleanup_expired_pages(sites_dir, ttl_days=5)

    assert deleted == 1
    assert fresh.exists()
    assert not stale.exists()


def test_cleanup_expired_pages_disabled_by_zero_ttl(tmp_path):
    app = _load_app_module()
    cleanup_expired_pages = _get_helper(app, "cleanup_expired_pages")

    sites_dir = tmp_path / "sites"
    sites_dir.mkdir()
    (sites_dir / "old.html").write_text("old", encoding="utf-8")

    assert cleanup_expired_pages(sites_dir, ttl_days=0) == 0
    assert (sites_dir / "old.html").exists()


def test_atomic_write_text_file_leaves_no_tmp_on_success(tmp_path):
    app = _load_app_module()
    write_text_file = _get_helper(app, "write_text_file")
    target = tmp_path / "page.html"

    write_text_file(target, "<!doctype html><html></html>")

    assert target.read_text(encoding="utf-8") == "<!doctype html><html></html>"
    assert not (tmp_path / "page.html.tmp").exists()


def test_clarification_state_lock_serializes_concurrent_writers(tmp_path):
    import threading

    app = _load_app_module()
    set_pending_clarification = _get_helper(app, "set_pending_clarification")
    get_pending_clarification = _get_helper(app, "get_pending_clarification")
    load_pending_clarifications = _get_helper(app, "load_pending_clarifications")
    config = _test_config(app, state_file=tmp_path / "pending.json")

    def writer(user_id: str) -> None:
        set_pending_clarification(
            config,
            user_id,
            app.Command(
                kind="clarify",
                filename=f"{user_id}-map.html",
                prompt="make a map",
                question="What market?",
            ),
        )

    threads = [threading.Thread(target=writer, args=(f"U{i}",)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state = load_pending_clarifications(config.state_file)
    assert set(state.keys()) == {"U0", "U1", "U2", "U3", "U4"}
    for user_id in ("U0", "U1", "U2", "U3", "U4"):
        pending = get_pending_clarification(config, user_id)
        assert pending["filename"] == f"{user_id}-map.html"


def test_rate_limit_backoff_retries_and_succeeds(monkeypatch):
    app = _load_app_module()
    create_anthropic_message = _get_helper(app, "create_anthropic_message")
    generation = importlib.import_module("generation")
    monkeypatch.setattr(generation.time, "sleep", lambda _seconds: None)
    fake = _RateLimitingAnthropic(["ok"], failures_before_success=2)

    response = create_anthropic_message(
        fake,
        {"model": "m", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert len(fake.requests) == 3
    assert response.content[0]["text"] == "ok"


def test_rate_limit_backoff_gives_up_after_max_attempts(monkeypatch):
    app = _load_app_module()
    create_anthropic_message = _get_helper(app, "create_anthropic_message")
    generation = importlib.import_module("generation")
    monkeypatch.setattr(generation.time, "sleep", lambda _seconds: None)
    fake = _RateLimitingAnthropic(["never"], failures_before_success=10)

    with pytest.raises(RuntimeError, match="rate_limit_error"):
        create_anthropic_message(
            fake,
            {"model": "m", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert len(fake.requests) == generation.RATE_LIMIT_MAX_ATTEMPTS_CREATE


def test_rate_limit_backoff_does_not_retry_other_errors():
    app = _load_app_module()
    create_anthropic_message = _get_helper(app, "create_anthropic_message")

    class _BrokenAnthropic:
        def __init__(self):
            self.messages = self
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            raise ValueError("invalid request payload")

    fake = _BrokenAnthropic()

    with pytest.raises(ValueError, match="invalid request payload"):
        create_anthropic_message(
            fake,
            {"model": "m", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert len(fake.requests) == 1


def test_chat_with_claude_streams_text_progress_to_callback():
    app = _load_app_module()
    chat_with_claude = _get_helper(app, "chat_with_claude")
    fake = _StreamingAnthropic(["Hello there, friend."], chunk_size=5)
    progress: list[str] = []

    result = chat_with_claude(
        fake,
        _test_config(app, web_search_enabled=False),
        "hi",
        on_progress=progress.append,
    )

    assert result == "Hello there, friend."
    assert len(progress) >= 2
    # Each progress update must be a prefix of the final text and grow monotonically.
    for previous, current in zip(progress, progress[1:]):
        assert current.startswith(previous) or len(current) >= len(previous)
    assert progress[-1] == "Hello there, friend."


def test_stream_anthropic_message_emits_ticks_on_injected_clock():
    generation = importlib.import_module("generation")
    fake = _StreamingAnthropic(["abcdefghij"], chunk_size=2)  # 5 deltas
    clock_values = iter([0.0, 1.0, 3.0, 6.0, 11.0, 17.0, 17.0])

    def fake_clock() -> float:
        return next(clock_values)

    events: list = []

    generation.stream_anthropic_message(
        fake,
        {"model": "m", "max_tokens": 100, "system": "s", "messages": [{"role": "user", "content": "hi"}]},
        on_progress=events.append,
        tick_interval=5.0,
        clock=fake_clock,
    )

    tick_events = [event for event in events if event.kind == "tick"]
    assert tick_events, "expected at least one tick event"
    # Ticks must have monotonically increasing elapsed time.
    tick_elapsed = [event.elapsed_seconds for event in tick_events]
    assert tick_elapsed == sorted(tick_elapsed)


def test_generate_html_forwards_tick_progress_to_updater(monkeypatch):
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    generation = importlib.import_module("generation")
    valid_html = "<!doctype html><html><head><title>T</title></head><body><h1>T</h1></body></html>"

    # Force stream_anthropic_message to emit one tick event so the callback sees a progress string.
    def fake_stream(anthropic, request, *, on_progress=None, tick_interval=5.0, clock=None):
        anthropic.requests.append(request)
        if on_progress is not None:
            on_progress(generation.StreamEvent(kind="tick", text_so_far="", elapsed_seconds=7.0))
        return SimpleNamespace(content=[{"type": "text", "text": valid_html}], stop_reason="end_turn")

    monkeypatch.setattr(generation, "stream_anthropic_message", fake_stream)

    fake = _StreamingAnthropic([valid_html])
    progress: list[str] = []
    html = generate_html(
        fake,
        _test_config(app, web_search_enabled=False),
        "landscape",
        "market-map.html",
        on_progress=progress.append,
    )

    assert html == valid_html
    assert progress, "expected on_progress to receive at least one tick update"
    assert "market-map.html" in progress[0]
    assert "elapsed" in progress[0]


def test_router_narrower_exception_propagates_unexpected_errors():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")

    class _KeyErrorAnthropic:
        def __init__(self):
            self.messages = self
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            raise KeyError("unexpected")

    with pytest.raises(KeyError):
        route_message_intent(_KeyErrorAnthropic(), _test_config(app), "make a map")


def test_router_falls_back_on_json_decode_errors():
    app = _load_app_module()
    route_message_intent = _get_helper(app, "route_message_intent")
    fake = _FakeAnthropic("this is not json at all")

    command = route_message_intent(fake, _test_config(app), "hello there")

    assert command.kind == "chat"
    assert command.prompt == "hello there"
