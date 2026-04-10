import html as html_escape
import json
import logging
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HELP_TEXT = (
    "*TangoBot usage guide*\n\n"
    "*Chat with Claude*\n"
    "Ask normal questions, brainstorm, summarize, compare options, or research current topics. "
    "If current facts matter, I can use Anthropic web search when it is enabled.\n\n"
    "*Generate and host a page*\n"
    "Describe the page you want and I will create a self-contained HTML page, save it, and reply with a Tailscale URL.\n"
    "Examples:\n"
    "- `make me a marketplace map for enterprise AI`\n"
    "- `build a pricing dashboard for our Q2 packaging options`\n"
    "- `create market-map.html for enterprise AI startups`\n"
    "- `generate market-map.html enterprise AI landscape with categories, companies, and funding`\n"
    "- `generate market-map.html`\n\n"
    "*Use your own source material*\n"
    "Paste notes, company lists, links, or data directly into the DM. "
    "You can also attach `.txt`, `.md`, `.csv`, or `.json` files with a request, and I will use those files as the primary source material.\n\n"
    "*Publish an existing HTML file*\n"
    "Upload a single `.html` file in this DM and I will publish it directly.\n\n"
    "*Filenames and access*\n"
    "Generated pages get readable filenames automatically. Uploaded or requested filenames are prefixed with your Slack user ID to avoid collisions. "
    "Anyone on the company tailnet can view the returned link.\n\n"
    "*Useful commands*\n"
    "- `help`\n"
    "- `guide`\n"
    "- `usage`"
)

GENERATION_SYSTEM_PROMPT = (
    "Generate a complete, polished, self-contained HTML document from the user's request. "
    "If the request is brief, infer a useful structure and include realistic illustrative content. "
    "When current facts, companies, funding, market landscapes, pricing, news, or dates matter, use web search. "
    "Treat user-provided notes, pasted lists, uploaded files, and URLs as primary source material. "
    "Use web search to fill gaps, verify current facts, and add citations when helpful. "
    "Prefer current, specific, verifiable information over generic filler. "
    "Do not ask follow-up questions. "
    "Avoid empty placeholders such as TODO, lorem ipsum, or coming soon. "
    "When using web information, include a concise Sources section with clickable links. "
    "Make the page responsive and immediately useful. "
    "Return only raw HTML. "
    "Do not wrap the response in Markdown fences. "
    "Inline all CSS and JavaScript."
)

CHAT_SYSTEM_PROMPT = (
    "You are Claude inside a Slack DM. Be concise, useful, and direct. "
    "Use web search when current facts, companies, pricing, news, or dates matter. "
    "If the user appears to want a hosted web page, dashboard, market map, report page, demo, or HTML artifact, "
    "tell them you can generate it and ask them to phrase it as a page request if needed."
)

ROUTER_SYSTEM_PROMPT = (
    "Route this Slack DM for a bot that can either chat normally or generate and host a single-file HTML page. "
    "Return only JSON with this schema: "
    '{"action":"chat|generate","filename":"optional-name.html or null","prompt":"the complete prompt to answer or generate from"}. '
    "Use generate when the user asks to make, create, build, design, draft, visualize, map, chart, dashboard, report, "
    "landing page, web page, HTML page, tool, demo, or other hosted artifact. "
    "Use chat for questions, setup help, explanations, and ordinary conversation. "
    "If generating and no filename is provided, set filename to null. "
    "If the user provides a filename ending in .html, preserve it."
)

SOURCE_FILE_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json"}
MAX_SOURCE_FILE_CHARS = 20000
MAX_TOTAL_SOURCE_CHARS = 60000
MAX_SLACK_MESSAGE_CHARS = 3500
HELP_ALIASES = {
    "help",
    "guide",
    "usage",
    "instructions",
    "how do i use this",
    "how do i use this?",
    "how does this work",
    "how does this work?",
    "what can you do",
    "what can you do?",
}


@dataclass(frozen=True)
class AppConfig:
    slack_bot_token: str
    slack_app_token: str
    anthropic_api_key: str
    anthropic_model: str
    sites_dir: Path
    tailscale_bin: str
    tailscale_base_url: str
    web_search_enabled: bool
    web_search_max_uses: int


@dataclass(frozen=True)
class Command:
    kind: str
    filename: str | None = None
    prompt: str | None = None


def require_env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "page"


def normalize_html_filename(name: str) -> str:
    base_name = Path(name).name
    stem = Path(base_name).stem
    return f"{slugify(stem)}.html"


def build_site_filename(slack_user_id: str, requested_name: str) -> str:
    normalized = normalize_html_filename(requested_name)
    return f"{slack_user_id}-{normalized}"


build_storage_filename = build_site_filename


FILENAME_STOPWORDS = {
    "a",
    "an",
    "and",
    "build",
    "create",
    "design",
    "draft",
    "for",
    "generate",
    "give",
    "html",
    "make",
    "me",
    "page",
    "please",
    "site",
    "the",
    "to",
    "with",
}


def filename_from_prompt(prompt: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", prompt.lower())
        if word not in FILENAME_STOPWORDS
    ]
    return f"{'-'.join(words[:6]).strip('-') or 'page'}.html"


def prompt_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    title = re.sub(r"[^A-Za-z0-9]+", " ", stem).strip() or "page"
    return f"Create a polished single-page web page for: {title}."


def filename_from_source_files(files: list[dict[str, Any]]) -> str:
    first_name = files[0].get("name", "source") if files else "source"
    return normalize_html_filename(first_name)


def prompt_from_source_filenames(files: list[dict[str, Any]]) -> str:
    names = [Path(file_obj.get("name", "source")).stem for file_obj in files]
    title = ", ".join(re.sub(r"[^A-Za-z0-9]+", " ", name).strip() for name in names if name)
    return f"Create a polished single-page web page from the attached source material: {title or 'sources'}."


def is_thin_prompt(prompt: str) -> bool:
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    return not words or all(word in FILENAME_STOPWORDS for word in words)


def extract_requested_filename(text: str) -> tuple[str | None, str]:
    match = re.search(r"([A-Za-z0-9][A-Za-z0-9_.-]*\.html)\b", text)
    if not match:
        return None, text.strip()

    filename = normalize_html_filename(match.group(1))
    prompt = re.sub(r"\s+", " ", f"{text[:match.start()]} {text[match.end():]}").strip()
    prompt = re.sub(r"^(create|make|build|generate|design|draft)\s+(for\s+)?", "", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"^for\s+", "", prompt, flags=re.IGNORECASE)
    return filename, prompt


def local_generation_hint(text: str) -> Command | None:
    filename, prompt = extract_requested_filename(text)
    if filename:
        return Command(
            kind="generate",
            filename=filename,
            prompt=prompt if not is_thin_prompt(prompt) else prompt_from_filename(filename),
        )
    return None


def parse_command(text: str | None) -> Command | None:
    if not text or not text.strip():
        return None

    stripped = text.strip()
    if stripped.lower() in HELP_ALIASES:
        return Command(kind="help")

    match = re.match(r"^generate\s+(\S+\.html)(?:\s+(.+))?$", stripped, re.IGNORECASE | re.DOTALL)
    if match:
        filename = normalize_html_filename(match.group(1))
        prompt = (match.group(2) or "").strip()
        return Command(
            kind="generate",
            filename=filename,
            prompt=prompt if not is_thin_prompt(prompt) else prompt_from_filename(filename),
        )

    hinted_command = local_generation_hint(stripped)
    if hinted_command:
        return hinted_command

    return Command(kind="route", prompt=stripped)


def looks_like_generation_request(text: str) -> bool:
    lowered = text.lower()
    has_creation_verb = re.search(r"\b(make|create|build|design|draft|generate|visualize|map|chart)\b", lowered)
    has_artifact_noun = re.search(
        r"\b(page|site|website|html|dashboard|map|chart|report|visualization|tool|demo|matrix|landscape)\b",
        lowered,
    )
    return bool(has_creation_verb and has_artifact_noun)


def fallback_route_message_intent(text: str) -> Command:
    if looks_like_generation_request(text):
        return Command(kind="generate", filename=filename_from_prompt(text), prompt=text)
    return Command(kind="chat", prompt=text)


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = strip_markdown_fences(text).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))

    if not isinstance(payload, dict):
        raise ValueError("Router response was not a JSON object.")
    return payload


def command_from_route_payload(payload: dict[str, Any], original_text: str) -> Command:
    action = str(payload.get("action", "")).strip().lower()
    prompt = str(payload.get("prompt") or original_text).strip()
    filename_value = payload.get("filename")
    filename = normalize_html_filename(str(filename_value)) if filename_value else None

    if action == "generate":
        return Command(kind="generate", filename=filename or filename_from_prompt(prompt), prompt=prompt)
    if action == "chat":
        return Command(kind="chat", prompt=prompt)

    return fallback_route_message_intent(original_text)


def extract_text_content(blocks: list[Any]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        block_type = get_block_value(block, "type")
        if block_type == "text":
            text_parts.append(get_block_value(block, "text", ""))
    return "\n".join(text_parts).strip()


def get_block_value(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def extract_web_search_errors(blocks: list[Any]) -> list[str]:
    errors: list[str] = []
    for block in blocks:
        if get_block_value(block, "type") != "web_search_tool_result":
            continue
        content = get_block_value(block, "content", {})
        if get_block_value(content, "type") == "web_search_tool_result_error":
            errors.append(get_block_value(content, "error_code", "unknown_error"))
    return errors


def extract_cited_sources(blocks: list[Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for block in blocks:
        for citation in get_block_value(block, "citations", []) or []:
            url = get_block_value(citation, "url") or get_block_value(citation, "source")
            if not url or url in seen_urls:
                continue

            seen_urls.add(url)
            sources.append(
                {
                    "url": str(url),
                    "title": str(get_block_value(citation, "title", "") or url),
                }
            )

    return sources


def append_sources_section(html: str, sources: list[dict[str, str]]) -> str:
    if not sources:
        return html

    source_items = "\n".join(
        (
            '<li><a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a></li>'
        ).format(
            url=html_escape.escape(source["url"], quote=True),
            title=html_escape.escape(source["title"]),
        )
        for source in sources
    )
    section = (
        "\n<section id=\"sources\" style=\"margin: 3rem auto; max-width: 960px; "
        "padding: 1rem; font-family: inherit;\">\n"
        "<h2>Sources</h2>\n"
        f"<ol>{source_items}</ol>\n"
        "</section>\n"
    )

    body_close = re.search(r"</body\s*>", html, re.IGNORECASE)
    if body_close:
        return f"{html[:body_close.start()]}{section}{html[body_close.start():]}"
    return f"{html}{section}"


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n\n[Truncated after {max_chars} characters.]"


def build_prompt_with_sources(prompt: str, source_files: list[dict[str, str]]) -> str:
    if not source_files:
        return prompt

    rendered_sources = []
    total_chars = 0
    for source_file in source_files:
        remaining_chars = MAX_TOTAL_SOURCE_CHARS - total_chars
        if remaining_chars <= 0:
            break

        content = truncate_text(source_file["content"], min(MAX_SOURCE_FILE_CHARS, remaining_chars))
        total_chars += len(content)
        rendered_sources.append(
            f"--- Source file: {source_file['name']} ---\n{content}"
        )

    return (
        f"User request:\n{prompt}\n\n"
        "Use the following user-provided source material as the primary factual basis for the page. "
        "You may use web search to verify or supplement it, but do not ignore these sources.\n\n"
        + "\n\n".join(rendered_sources)
    )


def command_for_source_generation(text: str | None, source_files: list[dict[str, Any]]) -> Command:
    command = parse_command(text)
    if command and command.kind != "route":
        return command

    prompt = command.prompt if command and command.prompt else prompt_from_source_filenames(source_files)
    filename = filename_from_prompt(prompt) if command and command.prompt else filename_from_source_files(source_files)
    return Command(kind="generate", filename=filename, prompt=prompt)


def write_text_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def run_tailscale_command(config: AppConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [config.tailscale_bin, *args],
        check=True,
        capture_output=True,
        text=True,
    )


def detect_tailscale_base_url(tailscale_bin: str) -> str:
    proc = subprocess.run(
        [tailscale_bin, "status", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    dns_name = payload.get("Self", {}).get("DNSName", "").rstrip(".")
    if not dns_name:
        raise RuntimeError("Unable to determine Tailscale DNS name from `tailscale status --json`.")
    return f"https://{dns_name}"


def load_config() -> AppConfig:
    load_env_file(Path(".env"))
    sites_dir = Path(os.environ.get("SITES_DIR", Path.home() / "sites")).expanduser().resolve()
    sites_dir.mkdir(parents=True, exist_ok=True)

    tailscale_bin = os.environ.get("TAILSCALE_BIN", "tailscale")
    tailscale_base_url = os.environ.get("TAILSCALE_BASE_URL")
    if not tailscale_base_url:
        tailscale_base_url = detect_tailscale_base_url(tailscale_bin)

    return AppConfig(
        slack_bot_token=require_env("SLACK_BOT_TOKEN"),
        slack_app_token=require_env("SLACK_APP_TOKEN"),
        anthropic_api_key=require_env("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        sites_dir=sites_dir,
        tailscale_bin=tailscale_bin,
        tailscale_base_url=tailscale_base_url.rstrip("/"),
        web_search_enabled=env_bool("ANTHROPIC_WEB_SEARCH", True),
        web_search_max_uses=max(env_int("ANTHROPIC_WEB_SEARCH_MAX_USES", 5), 1),
    )


def ensure_tailscale_directory_serving(config: AppConfig) -> None:
    if env_bool("SKIP_TAILSCALE_SERVE", False):
        logging.info("Skipping tailscale serve because SKIP_TAILSCALE_SERVE is set.")
        return
    run_tailscale_command(config, "serve", "--bg", str(config.sites_dir))


def publish_url(config: AppConfig, stored_name: str) -> str:
    return f"{config.tailscale_base_url}/{stored_name}"


def web_search_tools(config: AppConfig) -> list[dict[str, Any]]:
    if not config.web_search_enabled:
        return []
    return [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": config.web_search_max_uses,
        }
    ]


def create_anthropic_message(anthropic: Any, request: dict[str, Any]) -> Any:
    response = anthropic.messages.create(**request)
    continuation_count = 0
    while getattr(response, "stop_reason", None) == "pause_turn" and continuation_count < 2:
        continuation_count += 1
        request["messages"].append({"role": "assistant", "content": response.content})
        response = anthropic.messages.create(**request)

    if getattr(response, "stop_reason", None) == "pause_turn":
        raise RuntimeError("Anthropic paused the response before completing.")
    return response


def route_message_intent(anthropic: Any, config: AppConfig, text: str) -> Command:
    hinted_command = local_generation_hint(text)
    if hinted_command:
        return hinted_command

    request = {
        "model": config.anthropic_model,
        "max_tokens": 600,
        "system": ROUTER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": text}],
    }

    try:
        response = create_anthropic_message(anthropic, request)
        payload = extract_json_object(extract_text_content(response.content))
        return command_from_route_payload(payload, text)
    except Exception:  # noqa: BLE001
        return fallback_route_message_intent(text)


def append_sources_to_slack_response(text: str, sources: list[dict[str, str]]) -> str:
    if not sources:
        return text

    source_lines = [
        f"- <{source['url']}|{source['title']}>"
        for source in sources[:8]
    ]
    return f"{text}\n\nSources:\n" + "\n".join(source_lines)


def truncate_slack_response(text: str) -> str:
    if len(text) <= MAX_SLACK_MESSAGE_CHARS:
        return text
    return f"{text[:MAX_SLACK_MESSAGE_CHARS].rstrip()}\n\n[Response truncated.]"


def chat_with_claude(anthropic: Any, config: AppConfig, prompt: str) -> str:
    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": 1600,
        "system": CHAT_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    tools = web_search_tools(config)
    if tools:
        request["tools"] = tools

    response = create_anthropic_message(anthropic, request)
    search_errors = extract_web_search_errors(response.content)
    if search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")

    text = extract_text_content(response.content)
    if not text:
        raise RuntimeError("Anthropic returned an empty response.")
    return truncate_slack_response(append_sources_to_slack_response(text, extract_cited_sources(response.content)))


def generate_html(anthropic: Any, config: AppConfig, prompt: str) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": 4096,
        "system": GENERATION_SYSTEM_PROMPT,
        "messages": messages,
    }
    tools = web_search_tools(config)
    if tools:
        request["tools"] = tools

    response = create_anthropic_message(anthropic, request)
    search_errors = extract_web_search_errors(response.content)
    if search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")

    html = strip_markdown_fences(extract_text_content(response.content))
    if not html:
        raise RuntimeError("Anthropic returned an empty response.")
    return append_sources_section(html, extract_cited_sources(response.content))


def resolve_file_download_url(client: Any, file_obj: dict[str, Any]) -> str:
    url = file_obj.get("url_private_download")
    if url:
        return url

    file_id = file_obj.get("id")
    if not file_id:
        raise RuntimeError("Slack file payload did not include a download URL or file ID.")

    info = client.files_info(file=file_id)
    resolved_file = info.get("file", {})
    url = resolved_file.get("url_private_download")
    if not url:
        raise RuntimeError(f"Slack file {file_id} is missing `url_private_download`.")
    return url


def download_slack_file(client: Any, bot_token: str, file_obj: dict[str, Any]) -> str:
    download_url = resolve_file_download_url(client, file_obj)
    request = urllib.request.Request(
        download_url,
        headers={"Authorization": f"Bearer {bot_token}"},
    )
    with urllib.request.urlopen(request) as response:
        return response.read().decode("utf-8", errors="replace")


def is_supported_upload(file_obj: dict[str, Any]) -> bool:
    return Path(file_obj.get("name", "")).suffix.lower() == ".html"


def is_supported_source_upload(file_obj: dict[str, Any]) -> bool:
    return Path(file_obj.get("name", "")).suffix.lower() in SOURCE_FILE_SUFFIXES


def should_ignore_message_event(event: dict[str, Any]) -> bool:
    if event.get("channel_type") != "im":
        return True
    if event.get("bot_id"):
        return True
    return bool(event.get("subtype") and event.get("subtype") != "file_share")


def create_slack_app(config: AppConfig) -> Any:
    from anthropic import Anthropic
    from slack_bolt import App

    anthropic = Anthropic(api_key=config.anthropic_api_key)
    app = App(token=config.slack_bot_token)

    @app.event("message")
    def handle_message_events(event: dict[str, Any], say: Any, client: Any, logger: Any) -> None:
        if should_ignore_message_event(event):
            return

        slack_user_id = event.get("user")
        if not slack_user_id:
            logger.warning("Skipping DM event without a user ID: %s", event)
            return

        files = event.get("files") or []
        if files:
            html_files = [file_obj for file_obj in files if is_supported_upload(file_obj)]
            source_files = [file_obj for file_obj in files if is_supported_source_upload(file_obj)]

            for file_obj in html_files:
                original_name = file_obj.get("name", "upload.html")
                stored_name = build_site_filename(slack_user_id, original_name)
                output_path = config.sites_dir / stored_name
                try:
                    html = download_slack_file(client, config.slack_bot_token, file_obj)
                    write_text_file(output_path, html)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to save uploaded file %s", original_name)
                    say(f"Failed to save `{original_name}`: {exc}")
                    continue

                say(f"Published `{stored_name}`: {publish_url(config, stored_name)}")

            if html_files:
                return

            if source_files:
                command = command_for_source_generation(event.get("text"), source_files)
                if command.kind == "help":
                    say(HELP_TEXT)
                    return

                source_materials: list[dict[str, str]] = []
                try:
                    for file_obj in source_files:
                        source_materials.append(
                            {
                                "name": file_obj.get("name", "source"),
                                "content": download_slack_file(client, config.slack_bot_token, file_obj),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to download source attachment")
                    say(f"Failed to read source attachment: {exc}")
                    return

                assert command.filename is not None
                assert command.prompt is not None
                stored_name = build_site_filename(slack_user_id, command.filename)
                output_path = config.sites_dir / stored_name

                try:
                    say(f"Generating `{stored_name}` from attached sources...")
                    html = generate_html(anthropic, config, build_prompt_with_sources(command.prompt, source_materials))
                    write_text_file(output_path, html)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to generate %s from source attachments", stored_name)
                    say(f"Generation failed for `{stored_name}`: {exc}")
                    return

                say(f"Published `{stored_name}`: {publish_url(config, stored_name)}")
                return

            say("Supported uploads are `.html` for publishing or `.txt`, `.md`, `.csv`, `.json` as generation sources.")
            return

        command = parse_command(event.get("text"))
        if command is None:
            return
        if command.kind == "help":
            say(HELP_TEXT)
            return
        if command.kind == "route":
            command = route_message_intent(anthropic, config, command.prompt or "")
        if command.kind == "chat":
            try:
                say(chat_with_claude(anthropic, config, command.prompt or ""))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to answer chat message")
                say(f"Chat failed: {exc}")
            return
        assert command.filename is not None
        assert command.prompt is not None

        stored_name = build_site_filename(slack_user_id, command.filename)
        output_path = config.sites_dir / stored_name

        try:
            say(f"Generating `{stored_name}`...")
            html = generate_html(anthropic, config, command.prompt)
            write_text_file(output_path, html)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to generate %s", stored_name)
            say(f"Generation failed for `{stored_name}`: {exc}")
            return

        say(f"Published `{stored_name}`: {publish_url(config, stored_name)}")

    return app


def main() -> None:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = load_config()
    ensure_tailscale_directory_serving(config)
    app = create_slack_app(config)
    logging.info("Serving %s at %s", config.sites_dir, config.tailscale_base_url)
    SocketModeHandler(app, config.slack_app_token).start()


if __name__ == "__main__":
    main()
