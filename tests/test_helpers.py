from __future__ import annotations

import importlib
import json
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


def _test_config(app, state_file=None, web_search_enabled=True):
    return app.AppConfig(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        anthropic_api_key="sk-ant-test",
        anthropic_model="claude-sonnet-4-6",
        sites_dir=app.Path("/tmp/sites"),
        tailscale_bin="tailscale",
        tailscale_base_url="https://example.ts.net",
        web_search_enabled=web_search_enabled,
        web_search_max_uses=3,
        state_file=state_file or app.Path("/tmp/tangobot-test-state.json"),
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


def test_generate_html_returns_valid_first_response_without_retry():
    app = _load_app_module()
    generate_html = _get_helper(app, "generate_html")
    valid_html = "<!doctype html><html><head><title>Map</title></head><body><h1>Market Map</h1></body></html>"
    fake_anthropic = _SequenceAnthropic([valid_html])

    html = generate_html(fake_anthropic, _test_config(app), "enterprise AI landscape", "market-map.html")

    assert html == valid_html
    assert len(fake_anthropic.requests) == 1


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
