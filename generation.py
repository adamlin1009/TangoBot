import html as html_escape
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from config import (
    AppConfig,
    CHAT_MAX_TOKENS,
    GENERATION_MAX_TOKENS,
    MAX_MODEL_INPUT_CHARS,
    MAX_REVISION_CONTEXT_CHARS,
    MAX_REVISION_HTML_CHARS,
    MAX_REVISION_PATCH_HTML_CHARS,
    MAX_SLACK_MESSAGE_CHARS,
    MAX_SOURCE_FILE_CHARS,
    MAX_TOTAL_SOURCE_CHARS,
    REVISION_PATCH_MAX_TOKENS,
)


GENERATION_SYSTEM_PROMPT = (
    "Generate a complete, polished, self-contained HTML document from the user's request. "
    "The first bytes of your response must be <!doctype html>. "
    "Return only raw HTML; never include progress narration, research narration, explanations, or Markdown fences. "
    "If the request is brief but specific enough, infer a useful structure and include realistic illustrative content. "
    "Treat user-provided notes, pasted lists, uploaded files, and URLs as primary source material. "
    "Do not ask follow-up questions. "
    "Avoid empty placeholders such as TODO, lorem ipsum, or coming soon. "
    "Make the page responsive and immediately useful. "
    "Inline all CSS and JavaScript."
)

GENERATION_NO_SEARCH_SYSTEM_PROMPT = (
    "Use only the user's request, provided source material, and general model knowledge. "
    "Do not claim to have browsed the web, checked live sources, or verified current facts. "
    "If fresh market data is not supplied, keep specific company, funding, pricing, and date-sensitive claims clearly illustrative."
)

GENERATION_WEB_SEARCH_SYSTEM_PROMPT = (
    "Web search is available. When current facts, companies, funding, market landscapes, pricing, news, or dates matter, "
    "use web search to fill gaps, verify current facts, and add citations when helpful. "
    "When using web information, include a concise Sources section with clickable links."
)

CHAT_SYSTEM_PROMPT = (
    "You are Claude inside a Slack DM. Be concise, useful, and direct. "
    "If the user appears to want a hosted web page, dashboard, market map, report page, demo, or HTML artifact, "
    "tell them you can generate it and ask them to phrase it as a page request if needed."
)

CHAT_NO_SEARCH_SYSTEM_PROMPT = (
    "Use only the conversation and provided source material. "
    "Do not claim to have browsed the web or checked live sources."
)

CHAT_WEB_SEARCH_SYSTEM_PROMPT = (
    "Web search is available. Use it when current facts, companies, pricing, news, or dates matter."
)

HTML_REPAIR_PROMPT = (
    "Your previous response could not be published because it was not a complete raw HTML document. "
    "Return a complete, self-contained HTML document now. The first bytes must be <!doctype html>. "
    "Keep the page compact enough to finish within the output budget, and close both </body> and </html>. "
    "Do not include explanations, progress notes, Markdown fences, or any text outside the HTML document."
)

HTML_CONTINUATION_PROMPT = (
    "Continue the same HTML document from the exact next byte. "
    "Do not repeat any previous HTML, do not add explanations, and continue until the document is complete with </html>."
)

REVISION_PATCH_SYSTEM_PROMPT = (
    "Revise the supplied HTML by returning JSON operations only. "
    "Do not return HTML, Markdown, commentary, or code fences. "
    "Use exact substrings copied from the current HTML so the caller can apply the patch safely. "
    "Return this schema: "
    '{"operations":[{"op":"replace","old":"exact existing string","new":"replacement string"},'
    '{"op":"insert_before","anchor":"exact existing string","content":"inserted string"},'
    '{"op":"insert_after","anchor":"exact existing string","content":"inserted string"}],'
    '"fallback":null}. '
    "Use the smallest operation set that satisfies the revision. "
    "If an exact safe patch is not possible, return {\"operations\":[],\"fallback\":\"reason\"}."
)

BROAD_REVISION_PATTERN = re.compile(
    r"\b("
    r"redesign|rebuild|recreate|rewrite|start over|from scratch|entire page|whole page|"
    r"completely|different page|new page|turn (it|this|that|the page) into|convert (it|this|that|the page) into"
    r")\b",
    re.IGNORECASE,
)

MAX_HTML_CONTINUATIONS = 2

RATE_LIMIT_ERROR_PATTERN = re.compile(
    r"\b(429|rate[_ -]?limit|input tokens per minute|tokens per minute|requests per minute)\b",
    re.IGNORECASE,
)

RATE_LIMIT_BACKOFF_DELAYS = (1.0, 3.0, 7.0)
RATE_LIMIT_MAX_ATTEMPTS_CREATE = 3
RATE_LIMIT_MAX_ATTEMPTS_STREAM = 2


@dataclass(frozen=True)
class StreamEvent:
    kind: str  # "delta" | "tick"
    text_so_far: str
    elapsed_seconds: float


def truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n\n[Truncated after {max_chars} characters.]"


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def get_block_value(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def extract_text_content(blocks: list[Any]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        block_type = get_block_value(block, "type")
        if block_type == "text":
            text_parts.append(get_block_value(block, "text", ""))
    return "\n".join(text_parts).strip()


def extract_raw_text_content(blocks: list[Any]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        block_type = get_block_value(block, "type")
        if block_type == "text":
            text_parts.append(get_block_value(block, "text", ""))
    return "\n".join(text_parts)


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


def merge_sources(*source_groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for sources in source_groups:
        for source in sources:
            url = source.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(source)
    return merged


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


def extract_html_document(text: str) -> str:
    stripped = strip_markdown_fences(text)
    start_match = re.search(r"<!doctype\s+html\b|<html\b", stripped, re.IGNORECASE)
    if not start_match:
        raise ValueError("Anthropic did not return an HTML document.")

    candidate = stripped[start_match.start():]
    end_matches = list(re.finditer(r"</html\s*>", candidate, re.IGNORECASE))
    if not end_matches:
        raise ValueError("Anthropic returned incomplete HTML without a closing </html> tag.")

    html = candidate[: end_matches[-1].end()].strip()
    required_tags = {
        "html": r"<html\b",
        "head": r"<head\b",
        "body": r"<body\b",
    }
    missing = [
        tag
        for tag, pattern in required_tags.items()
        if not re.search(pattern, html, re.IGNORECASE)
    ]
    if missing:
        raise ValueError(f"Anthropic returned incomplete HTML missing: {', '.join(missing)}.")
    return html


def build_prompt_with_sources(prompt: str, source_files: list[dict[str, str]]) -> str:
    bounded_prompt = truncate_text(prompt, MAX_MODEL_INPUT_CHARS)
    if not source_files:
        return bounded_prompt

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
        f"User request:\n{bounded_prompt}\n\n"
        "Use the following user-provided source material as the primary factual basis for the page. "
        "Do not ignore these sources or replace them with generic filler.\n\n"
        + "\n\n".join(rendered_sources)
    )


def build_generation_prompt(prompt: str, filename: str | None = None) -> str:
    # Local import avoids commands <-> generation cycle at module load time.
    from commands import title_from_filename

    title = title_from_filename(filename)
    filename_line = f"Requested filename: {filename}\n" if filename else ""
    bounded_prompt = truncate_text(prompt, MAX_MODEL_INPUT_CHARS)
    return (
        f"{filename_line}"
        f"User request:\n{bounded_prompt}\n\n"
        "Build the actual requested artifact as a finished single-page HTML document. "
        "Infer the artifact type, audience, core content, layout, and useful interactions from the request. "
        "Use the requested filename as intent context, but do not show the filename as a title unless it is natural.\n\n"
        "If the request is brief, choose practical defaults and make the page immediately useful instead of generic. "
        "Maps should become structured visual landscapes; dashboards should become data-oriented views; "
        "tools should be usable on the first screen; reports and plans should be scannable, specific, and organized. "
        "Use source material supplied by the user for factual details and avoid unsupported claims about current facts.\n\n"
        f"Working title or inferred topic: {title}."
    )


def build_revision_prompt(entry: dict[str, Any], instructions: str, current_html: str) -> str:
    requested_filename = str(entry.get("requested_filename") or entry.get("stored_name") or "page.html")
    original_prompt = truncate_text(str(entry.get("original_prompt") or ""), MAX_REVISION_CONTEXT_CHARS)
    last_prompt = truncate_text(str(entry.get("last_prompt") or ""), MAX_REVISION_CONTEXT_CHARS)
    bounded_instructions = truncate_text(instructions.strip(), MAX_REVISION_CONTEXT_CHARS)
    bounded_html = truncate_text(current_html, MAX_REVISION_HTML_CHARS)
    source_filenames = entry.get("source_filenames") if isinstance(entry.get("source_filenames"), list) else []
    source_line = f"Source files used previously: {', '.join(map(str, source_filenames))}\n" if source_filenames else ""

    return (
        "Revise the existing published HTML page while preserving the user's intent and improving only what was requested.\n\n"
        f"Requested filename: {requested_filename}\n"
        f"{source_line}"
        "Original request:\n"
        f"{original_prompt or '(not recorded)'}\n\n"
        "Most recent request or revision:\n"
        f"{last_prompt or '(not recorded)'}\n\n"
        "Revision instructions:\n"
        f"{bounded_instructions}\n\n"
        "Current live HTML:\n"
        f"{bounded_html}\n\n"
        "Return a complete replacement HTML document. Keep the same overall artifact unless the revision explicitly asks for a change."
    )


def build_revision_patch_prompt(entry: dict[str, Any], instructions: str, current_html: str) -> str:
    requested_filename = str(entry.get("requested_filename") or entry.get("stored_name") or "page.html")
    original_prompt = truncate_text(str(entry.get("original_prompt") or ""), MAX_REVISION_CONTEXT_CHARS)
    last_prompt = truncate_text(str(entry.get("last_prompt") or ""), MAX_REVISION_CONTEXT_CHARS)
    bounded_instructions = truncate_text(instructions.strip(), MAX_REVISION_CONTEXT_CHARS)
    bounded_html = truncate_text(current_html, MAX_REVISION_PATCH_HTML_CHARS)
    source_filenames = entry.get("source_filenames") if isinstance(entry.get("source_filenames"), list) else []
    source_line = f"Source files used previously: {', '.join(map(str, source_filenames))}\n" if source_filenames else ""

    return (
        "Create a safe patch for this published HTML page.\n\n"
        f"Requested filename: {requested_filename}\n"
        f"{source_line}"
        "Original request:\n"
        f"{original_prompt or '(not recorded)'}\n\n"
        "Most recent request or revision:\n"
        f"{last_prompt or '(not recorded)'}\n\n"
        "Revision instructions:\n"
        f"{bounded_instructions}\n\n"
        "Current live HTML:\n"
        f"{bounded_html}\n\n"
        "Return JSON operations only. Prefer one or a few exact replacements or insertions."
    )


def is_broad_revision_request(instructions: str) -> bool:
    return bool(BROAD_REVISION_PATTERN.search(instructions.strip()))


def extract_json_payload(text: str) -> dict[str, Any]:
    stripped = strip_markdown_fences(text).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Patch response was not JSON.") from None
        try:
            payload = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("Patch response was not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Patch response was not a JSON object.")
    return payload


def _string_field(operation: dict[str, Any], field: str) -> str:
    value = operation.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Patch operation missing string field: {field}.")
    return value


def _single_match_index(html: str, needle: str) -> int:
    count = html.count(needle)
    if count != 1:
        raise ValueError(f"Patch anchor matched {count} times instead of exactly once.")
    return html.find(needle)


def apply_revision_operations(current_html: str, operations: list[dict[str, Any]]) -> str:
    if not operations:
        raise ValueError("Patch response did not include any operations.")

    html = current_html
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Patch operation was not an object.")

        op = str(operation.get("op") or "")
        if op == "replace":
            old = _string_field(operation, "old")
            new = operation.get("new")
            if not isinstance(new, str):
                raise ValueError("Patch replace operation missing string field: new.")
            index = _single_match_index(html, old)
            html = f"{html[:index]}{new}{html[index + len(old):]}"
            continue

        if op == "insert_before":
            anchor = _string_field(operation, "anchor")
            content = _string_field(operation, "content")
            index = _single_match_index(html, anchor)
            html = f"{html[:index]}{content}{html[index:]}"
            continue

        if op == "insert_after":
            anchor = _string_field(operation, "anchor")
            content = _string_field(operation, "content")
            index = _single_match_index(html, anchor) + len(anchor)
            html = f"{html[:index]}{content}{html[index:]}"
            continue

        raise ValueError(f"Unsupported patch operation: {op or '(missing)'}.")

    return extract_html_document(html)


def parse_revision_patch(text: str) -> list[dict[str, Any]]:
    payload = extract_json_payload(text)
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise ValueError("Patch response missing operations list.")
    if not operations and payload.get("fallback"):
        raise ValueError(str(payload["fallback"]))
    return operations


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


def generation_system_prompt(config: AppConfig) -> str:
    search_guidance = (
        GENERATION_WEB_SEARCH_SYSTEM_PROMPT
        if config.web_search_enabled
        else GENERATION_NO_SEARCH_SYSTEM_PROMPT
    )
    return f"{GENERATION_SYSTEM_PROMPT} {search_guidance}"


def chat_system_prompt(config: AppConfig) -> str:
    search_guidance = (
        CHAT_WEB_SEARCH_SYSTEM_PROMPT
        if config.web_search_enabled
        else CHAT_NO_SEARCH_SYSTEM_PROMPT
    )
    return f"{CHAT_SYSTEM_PROMPT} {search_guidance}"


def is_rate_limit_error(exc: BaseException) -> bool:
    error_text = f"{exc.__class__.__name__}: {exc}"
    return bool(RATE_LIMIT_ERROR_PATTERN.search(error_text))


def generation_failure_message(stored_name: str, exc: BaseException) -> str:
    if is_rate_limit_error(exc):
        return (
            f"Generation hit the model rate limit for `{stored_name}`. "
            "Wait about a minute and try again with a narrower scope, or attach source data so I can avoid a broad research-style prompt."
        )
    return f"Generation failed for `{stored_name}`: {exc}"


def chat_failure_message(exc: BaseException) -> str:
    if is_rate_limit_error(exc):
        return "Chat hit the model rate limit. Wait about a minute and try again with a shorter message."
    return f"Chat failed: {exc}"


def revision_failure_message(exc: BaseException) -> str:
    if is_rate_limit_error(exc):
        return "Revision hit the model rate limit. Wait about a minute and try again with a narrower change."
    if "too large to complete" in str(exc) or "output token limit" in str(exc):
        return (
            "Revision could not fit into the model output budget after automatic continuation. "
            "Try a narrower change, or ask for a simpler compact version of the page."
        )
    return f"Revision failed: {exc}"


def html_repair_prompt(first_error: ValueError, response: Any) -> str:
    if getattr(response, "stop_reason", None) == "max_tokens":
        return (
            f"{HTML_REPAIR_PROMPT}\n\n"
            "The previous response hit the output token limit before completion. "
            "Generate a shorter version with fewer sections, smaller datasets, and less inline JavaScript.\n\n"
            f"Validation error: {first_error}"
        )
    return f"{HTML_REPAIR_PROMPT}\n\nValidation error: {first_error}"


def output_token_limit_message() -> str:
    return (
        "The page is too large to complete in one model response even after automatic continuation. "
        "I tried a compact repair pass, but Anthropic still stopped before the HTML could be closed."
    )


def extract_html_response_text(responses: list[Any]) -> str:
    return "".join(extract_raw_text_content(response.content) for response in responses)


def extract_html_response_sources(responses: list[Any]) -> list[dict[str, str]]:
    return merge_sources(*(extract_cited_sources(response.content) for response in responses))


def response_has_web_search_errors(response: Any) -> list[str]:
    return extract_web_search_errors(response.content)


def _with_rate_limit_retry(fn: Callable[[], Any], *, max_attempts: int) -> Any:
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if not is_rate_limit_error(exc) or attempt + 1 >= max_attempts:
                raise
            base_delay = RATE_LIMIT_BACKOFF_DELAYS[min(attempt, len(RATE_LIMIT_BACKOFF_DELAYS) - 1)]
            jitter = base_delay * random.uniform(-0.3, 0.3)
            time.sleep(max(0.1, base_delay + jitter))
    raise RuntimeError("rate-limit retry loop exited without returning")


def create_anthropic_message(anthropic: Any, request: dict[str, Any]) -> Any:
    response = _with_rate_limit_retry(
        lambda: anthropic.messages.create(**request),
        max_attempts=RATE_LIMIT_MAX_ATTEMPTS_CREATE,
    )
    continuation_count = 0
    while getattr(response, "stop_reason", None) == "pause_turn" and continuation_count < 2:
        continuation_count += 1
        request["messages"].append({"role": "assistant", "content": response.content})
        response = _with_rate_limit_retry(
            lambda: anthropic.messages.create(**request),
            max_attempts=RATE_LIMIT_MAX_ATTEMPTS_CREATE,
        )

    if getattr(response, "stop_reason", None) == "pause_turn":
        raise RuntimeError("Anthropic paused the response before completing.")
    return response


def stream_anthropic_message(
    anthropic: Any,
    request: dict[str, Any],
    *,
    on_progress: Callable[[StreamEvent], None] | None = None,
    tick_interval: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    start = clock()
    state = {"last_tick_at": start, "text_so_far": ""}

    def run_stream() -> Any:
        with anthropic.messages.stream(**request) as stream:
            for text_delta in stream.text_stream:
                state["text_so_far"] += text_delta
                now = clock()
                if on_progress is not None:
                    on_progress(
                        StreamEvent(
                            kind="delta",
                            text_so_far=state["text_so_far"],
                            elapsed_seconds=now - start,
                        )
                    )
                if now - state["last_tick_at"] >= tick_interval:
                    state["last_tick_at"] = now
                    if on_progress is not None:
                        on_progress(
                            StreamEvent(
                                kind="tick",
                                text_so_far=state["text_so_far"],
                                elapsed_seconds=now - start,
                            )
                        )
            return stream.get_final_message()

    response = _with_rate_limit_retry(run_stream, max_attempts=RATE_LIMIT_MAX_ATTEMPTS_STREAM)
    continuation_count = 0
    while getattr(response, "stop_reason", None) == "pause_turn" and continuation_count < 2:
        continuation_count += 1
        request["messages"].append({"role": "assistant", "content": response.content})
        response = _with_rate_limit_retry(run_stream, max_attempts=RATE_LIMIT_MAX_ATTEMPTS_STREAM)

    if getattr(response, "stop_reason", None) == "pause_turn":
        raise RuntimeError("Anthropic paused the response before completing.")
    return response


def chat_with_claude(
    anthropic: Any,
    config: AppConfig,
    prompt: str,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": CHAT_MAX_TOKENS,
        "system": chat_system_prompt(config),
        "messages": [{"role": "user", "content": truncate_text(prompt, MAX_MODEL_INPUT_CHARS)}],
    }
    tools = web_search_tools(config)
    if tools:
        request["tools"] = tools

    if on_progress is None:
        response = create_anthropic_message(anthropic, request)
    else:
        def forward(event: StreamEvent) -> None:
            if event.kind == "delta":
                on_progress(event.text_so_far)

        response = stream_anthropic_message(anthropic, request, on_progress=forward)

    search_errors = extract_web_search_errors(response.content)
    if search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")

    text = extract_text_content(response.content)
    if not text:
        raise RuntimeError("Anthropic returned an empty response.")
    return truncate_slack_response(append_sources_to_slack_response(text, extract_cited_sources(response.content)))


def _format_generation_tick(filename: str | None, phase: str, elapsed: float) -> str:
    label = f"`{filename}`" if filename else "page"
    return f"{phase} {label} \u2014 {int(elapsed)}s elapsed..."


def generate_html(
    anthropic: Any,
    config: AppConfig,
    prompt: str,
    filename: str | None = None,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    generation_prompt = build_generation_prompt(prompt, filename)
    messages: list[dict[str, Any]] = [{"role": "user", "content": generation_prompt}]
    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": config.generation_max_tokens,
        "system": generation_system_prompt(config),
        "messages": messages,
    }
    tools = web_search_tools(config)
    if tools:
        request["tools"] = tools

    def call_model(phase: str) -> Any:
        if on_progress is None:
            return create_anthropic_message(anthropic, request)

        def forward(event: StreamEvent) -> None:
            if event.kind == "tick":
                on_progress(_format_generation_tick(filename, phase, event.elapsed_seconds))

        return stream_anthropic_message(anthropic, request, on_progress=forward)

    def collect_complete_response(initial_response: Any, phase: str) -> tuple[str, list[dict[str, str]], Any]:
        responses = [initial_response]
        response = initial_response
        continuation_count = 0
        while getattr(response, "stop_reason", None) == "max_tokens" and continuation_count < MAX_HTML_CONTINUATIONS:
            continuation_count += 1
            request["messages"].append({"role": "assistant", "content": response.content})
            request["messages"].append({"role": "user", "content": HTML_CONTINUATION_PROMPT})
            response = call_model(phase)
            search_errors = response_has_web_search_errors(response)
            if search_errors:
                raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")
            responses.append(response)

        return extract_html_response_text(responses), extract_html_response_sources(responses), response

    response = call_model("Generating")
    search_errors = extract_web_search_errors(response.content)
    if search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")

    first_text, first_sources, first_last_response = collect_complete_response(response, "Continuing")
    try:
        html = extract_html_document(first_text)
        return append_sources_section(html, first_sources)
    except ValueError as first_error:
        request["messages"] = [
            {
                "role": "user",
                "content": f"{generation_prompt}\n\n{html_repair_prompt(first_error, first_last_response)}",
            }
        ]

    retry_response = call_model("Repairing")
    retry_search_errors = extract_web_search_errors(retry_response.content)
    if retry_search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(retry_search_errors)}")

    retry_text, retry_sources, retry_last_response = collect_complete_response(retry_response, "Continuing repair")
    try:
        html = extract_html_document(retry_text)
    except ValueError as retry_error:
        if getattr(retry_last_response, "stop_reason", None) == "max_tokens":
            raise RuntimeError(output_token_limit_message()) from retry_error
        raise RuntimeError(f"Anthropic returned invalid HTML after retry: {retry_error}") from retry_error

    sources = merge_sources(first_sources, retry_sources)
    return append_sources_section(html, sources)


def revise_html_with_patch(
    anthropic: Any,
    config: AppConfig,
    entry: dict[str, Any],
    instructions: str,
    current_html: str,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    if on_progress is not None:
        on_progress("Preparing a targeted patch...")

    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": REVISION_PATCH_MAX_TOKENS,
        "system": REVISION_PATCH_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": build_revision_patch_prompt(entry, instructions, current_html),
            }
        ],
    }
    response = create_anthropic_message(anthropic, request)
    text = extract_text_content(response.content)
    if not text:
        raise ValueError("Patch response was empty.")

    operations = parse_revision_patch(text)
    return apply_revision_operations(current_html, operations)


def revise_published_page(
    anthropic: Any,
    config: AppConfig,
    slack_user_id: str,
    target_filename: str | None,
    instructions: str,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    # Local imports keep storage -> commands -> generation cycle broken.
    from storage import record_page_publish, resolve_page_entry, write_text_file

    instructions = instructions.strip()
    if not instructions:
        raise ValueError("Tell me what to change, for example `revise it to make the layout cleaner`.")

    resolved = resolve_page_entry(config, slack_user_id, target_filename)
    if not resolved:
        raise LookupError("I could not find a published page to revise. Generate or upload a page first.")

    stored_name, entry = resolved
    live_path = config.sites_dir / stored_name
    if not live_path.exists():
        raise FileNotFoundError(f"The live page `{stored_name}` is missing from the sites directory.")

    current_html = live_path.read_text(encoding="utf-8")
    requested_filename = str(entry.get("requested_filename") or stored_name)
    if is_broad_revision_request(instructions):
        revision_prompt = build_revision_prompt(entry, instructions, current_html)
        html = generate_html(anthropic, config, revision_prompt, requested_filename, on_progress=on_progress)
    else:
        try:
            html = revise_html_with_patch(
                anthropic,
                config,
                entry,
                instructions,
                current_html,
                on_progress=on_progress,
            )
        except ValueError:
            revision_prompt = build_revision_prompt(entry, instructions, current_html)
            html = generate_html(anthropic, config, revision_prompt, requested_filename, on_progress=on_progress)

    write_text_file(live_path, html)
    return record_page_publish(
        config,
        slack_user_id,
        requested_filename,
        stored_name,
        html,
        instructions,
        publish_kind="revision",
        source_filenames=entry.get("source_filenames") if isinstance(entry.get("source_filenames"), list) else None,
    )


def detect_jsx_component_name(jsx_source: str) -> str | None:
    patterns = [
        r"\bfunction\s+(App)\s*\(",
        r"\bconst\s+(App)\s*=",
        r"\bclass\s+(App)\s+extends\b",
        r"\bexport\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)\s*\(",
        r"\bexport\s+default\s+class\s+([A-Z][A-Za-z0-9_]*)\s+extends\b",
        r"\bexport\s+default\s+([A-Z][A-Za-z0-9_]*)\s*;?",
        r"\bfunction\s+([A-Z][A-Za-z0-9_]*)\s*\(",
        r"\bconst\s+([A-Z][A-Za-z0-9_]*)\s*=",
        r"\bclass\s+([A-Z][A-Za-z0-9_]*)\s+extends\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, jsx_source)
        if match:
            return match.group(1)
    return None


def normalize_jsx_exports(jsx_source: str) -> str:
    normalized = re.sub(
        r"\bexport\s+default\s+function\s+([A-Z][A-Za-z0-9_]*)\s*\(",
        r"function \1(",
        jsx_source,
    )
    normalized = re.sub(
        r"\bexport\s+default\s+class\s+([A-Z][A-Za-z0-9_]*)\s+extends\b",
        r"class \1 extends",
        normalized,
    )
    normalized = re.sub(r"\bexport\s+default\s+[A-Z][A-Za-z0-9_]*\s*;?", "", normalized)
    return normalized


def validate_jsx_source(jsx_source: str) -> str:
    if re.search(r"^\s*import\s+", jsx_source, re.MULTILINE):
        raise ValueError("JSX uploads must be one self-contained component; `import` is not supported.")
    if re.search(r"\brequire\s*\(", jsx_source):
        raise ValueError("JSX uploads must be one self-contained component; `require(...)` is not supported.")
    if re.search(r"^\s*export\s+(?!default\b)", jsx_source, re.MULTILINE):
        raise ValueError("Only `export default` is supported in JSX uploads.")

    component_name = detect_jsx_component_name(jsx_source)
    if not component_name:
        raise ValueError(
            "Could not find a React component. Use `function App()`, `const App = ...`, "
            "`export default function App()`, or another PascalCase component."
        )
    return component_name


def jsx_runtime_error_script() -> str:
    return """
window.addEventListener("error", function(event) {
  var root = document.getElementById("root");
  if (!root) return;
  root.innerHTML = "<main class=\\"tangobot-error\\"><h1>JSX runtime error</h1><pre></pre></main>";
  root.querySelector("pre").textContent = event.message || String(event.error || "Unknown error");
});
""".strip()


def wrap_jsx_as_html(jsx_source: str, title: str) -> str:
    component_name = validate_jsx_source(jsx_source)
    normalized_source = normalize_jsx_exports(jsx_source).replace("</script", "<\\/script")
    escaped_title = html_escape.escape(title)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    html, body, #root {{
      min-height: 100%;
      margin: 0;
    }}
    body {{
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .tangobot-error {{
      max-width: 900px;
      margin: 48px auto;
      padding: 24px;
      border: 1px solid #f0b4b4;
      border-radius: 8px;
      background: #fff5f5;
      color: #5f1515;
    }}
    .tangobot-error pre {{
      white-space: pre-wrap;
    }}
  </style>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script>{jsx_runtime_error_script()}</script>
</head>
<body>
  <div id="root"></div>
  <script type="text/babel" data-presets="env,react">
{normalized_source}

const tangobotRoot = ReactDOM.createRoot(document.getElementById("root"));
tangobotRoot.render(<{component_name} />);
  </script>
</body>
</html>
"""
