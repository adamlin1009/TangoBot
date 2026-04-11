import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import AppConfig, MAX_ROUTER_INPUT_CHARS, ROUTER_MAX_TOKENS


logger = logging.getLogger(__name__)


ROUTER_SYSTEM_PROMPT = (
    "Route this Slack DM for a bot that can chat normally, ask one clarification question, or generate and host a single-file HTML page. "
    "Return only JSON with this schema: "
    '{"action":"chat|generate|clarify","filename":"optional-name.html or null","prompt":"the complete prompt to answer or generate from","question":"optional clarification question or null"}. '
    "Use generate when the user asks to make, create, build, design, draft, visualize, map, chart, dashboard, report, "
    "landing page, web page, HTML page, tool, demo, or other hosted artifact. "
    "Use clarify when the user wants an artifact but the subject, audience, data, or desired outcome is too underspecified to generate the right thing. "
    "Use chat for questions, setup help, explanations, and ordinary conversation. "
    "If generating and no filename is provided, set filename to null. "
    "If the user provides a filename ending in .html, preserve it."
)

SOURCE_FILE_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json"}
ARTIFACT_FILE_SUFFIXES = {".html", ".jsx"}
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
CANCEL_ALIASES = {"cancel", "nevermind", "never mind", "stop"}
GENERATION_ARTIFACT_TERMS = {
    "app",
    "application",
    "artifact",
    "brief",
    "chart",
    "dashboard",
    "demo",
    "document",
    "html",
    "landing",
    "landscape",
    "map",
    "market",
    "matrix",
    "page",
    "plan",
    "report",
    "site",
    "tool",
    "visualization",
    "website",
    "ecosystem",
    "marketplace",
}
GENERATION_FILLER_TERMS = {
    "all",
    "anything",
    "comprehensive",
    "current",
    "data",
    "everything",
    "good",
    "great",
    "info",
    "information",
    "latest",
    "nice",
    "polished",
    "relevant",
    "single",
    "stuff",
    "thing",
    "things",
    "use",
    "web",
    "search",
}
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
GENERATION_STOPWORDS = FILENAME_STOPWORDS | GENERATION_ARTIFACT_TERMS | GENERATION_FILLER_TERMS
BROAD_MARKET_MAP_TERMS = {
    "agent",
    "agents",
    "ai",
    "companies",
    "company",
    "enterprise",
    "enterprises",
    "marketplaces",
    "platform",
    "platforms",
    "products",
    "solutions",
    "software",
    "startup",
    "startups",
    "tool",
    "tools",
    "vendor",
    "vendors",
}
BROAD_MARKET_MAP_DETAIL_PATTERN = re.compile(
    r"\b("
    r"attached|based on|buyer|buyers|categories|category|columns|compare|comparison|csv|"
    r"funding|geography|include|including|investor|investors|json|pricing|rank|ranking|"
    r"region|rows|score|segment|segmented|stage|vertical|with"
    r")\b",
    re.IGNORECASE,
)
BROAD_MARKET_MAP_PATTERN = re.compile(
    r"\b(market\s+map|marketplace\s+map|landscape|matrix|ecosystem)\b",
    re.IGNORECASE,
)
REVISION_COMMAND_PATTERN = re.compile(r"^(revise|edit|update)\b(?:\s+(.+))?$", re.IGNORECASE | re.DOTALL)
REVISION_START_PATTERN = re.compile(
    r"^\s*(add|remove|delete|replace|change|use|switch|tweak|polish|simplify|include|exclude|highlight)\b",
    re.IGNORECASE,
)
REVISION_TARGET_PATTERN = re.compile(
    r"\b(it|this|that|last one|current page|the page|the map|the dashboard|the site|the last one)\b",
    re.IGNORECASE,
)
REVISION_ACTION_PATTERN = re.compile(
    r"\b(add|remove|delete|replace|change|revise|edit|update|tweak|polish|simplify|clean|cleaner|"
    r"use|turn|switch|highlight|include|exclude|shorter|longer|darker|lighter|more|less)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Command:
    kind: str
    filename: str | None = None
    prompt: str | None = None
    question: str | None = None


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "page"


def normalize_html_filename(name: str) -> str:
    base_name = Path(name).name
    stem = Path(base_name).stem
    return f"{slugify(stem)}.html"


def normalize_jsx_filename(name: str) -> str:
    base_name = Path(name).name
    stem = Path(base_name).stem
    return f"{slugify(stem)}.jsx"


def build_site_filename(slack_user_id: str, requested_name: str) -> str:
    normalized = normalize_html_filename(requested_name)
    return f"{slack_user_id}-{normalized}"


build_storage_filename = build_site_filename


def build_jsx_source_filename(slack_user_id: str, requested_name: str) -> str:
    return f"{slack_user_id}-{normalize_jsx_filename(requested_name)}"


def build_jsx_page_filename(slack_user_id: str, requested_name: str) -> str:
    return f"{slack_user_id}-{normalize_html_filename(requested_name)}"


def filename_from_prompt(prompt: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", prompt.lower())
        if word not in FILENAME_STOPWORDS
    ]
    return f"{'-'.join(words[:6]).strip('-') or 'page'}.html"


def title_from_filename(filename: str | None) -> str:
    if not filename:
        return "page"
    stem = Path(filename).stem
    return re.sub(r"[^A-Za-z0-9]+", " ", stem).strip() or "page"


def prompt_from_filename(filename: str) -> str:
    title = title_from_filename(filename)
    return f"Create a complete, useful single-page artifact inferred from the filename: {title}."


def generation_content_terms(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z0-9]+", text.lower())
        if word not in GENERATION_STOPWORDS
    ]


def is_broad_market_map_request(text: str) -> bool:
    if not BROAD_MARKET_MAP_PATTERN.search(text):
        return False
    if BROAD_MARKET_MAP_DETAIL_PATTERN.search(text):
        return False

    content_terms = generation_content_terms(text)
    if not content_terms:
        return True

    specific_terms = [
        word
        for word in content_terms
        if word not in BROAD_MARKET_MAP_TERMS
    ]
    return len(content_terms) <= 3 and not specific_terms


def should_clarify_generation_request(filename: str | None, prompt: str | None) -> bool:
    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return True
    if filename and prompt_text == prompt_from_filename(filename):
        return True

    words = re.findall(r"[a-z0-9]+", prompt_text.lower())
    if not words:
        return True
    if not generation_content_terms(prompt_text):
        return True
    return is_broad_market_map_request(prompt_text)


def clarification_question_for(filename: str | None, prompt: str | None) -> str:
    combined = f"{title_from_filename(filename)} {prompt or ''}".lower()
    if re.search(r"\b(map|landscape|matrix)\b", combined):
        if filename and (prompt or "").strip() == prompt_from_filename(filename):
            return "What market, industry, or audience should this map cover?"
        if generation_content_terms(prompt or ""):
            return "What audience, categories, or source material should this map use?"
        return "What market, industry, or audience should this map cover?"
    if re.search(r"\bdashboard\b", combined):
        return "What data, team, or business area should this dashboard cover?"
    if re.search(r"\b(chart|visualization)\b", combined):
        return "What data or topic should this visualization focus on?"
    if re.search(r"\b(tool|demo|app|application)\b", combined):
        return "What should this tool help someone do?"
    if re.search(r"\b(report|brief|plan|document)\b", combined):
        return "What topic and audience should this page focus on?"
    return "What topic, audience, or outcome should this page focus on?"


def generation_or_clarification_command(
    filename: str | None,
    prompt: str | None,
    *,
    question: str | None = None,
) -> Command:
    if should_clarify_generation_request(filename, prompt):
        resolved_filename = filename or filename_from_prompt(prompt or "page")
        resolved_prompt = prompt or prompt_from_filename(resolved_filename)
        return Command(
            kind="clarify",
            filename=resolved_filename,
            prompt=resolved_prompt,
            question=question or clarification_question_for(resolved_filename, resolved_prompt),
        )

    assert prompt is not None
    return Command(kind="generate", filename=filename or filename_from_prompt(prompt), prompt=prompt)


def build_prompt_from_clarification(pending: dict[str, Any], answer: str) -> tuple[str, str]:
    filename = str(pending.get("filename") or filename_from_prompt(answer))
    original_prompt = str(pending.get("prompt") or prompt_from_filename(filename))
    prompt = (
        "Original request:\n"
        f"{original_prompt}\n\n"
        "Clarification answer:\n"
        f"{answer.strip()}"
    )
    return filename, prompt


def is_cancel_text(text: str | None) -> bool:
    return (text or "").strip().lower() in CANCEL_ALIASES


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
        resolved_prompt = prompt if not is_thin_prompt(prompt) else prompt_from_filename(filename)
        return generation_or_clarification_command(filename, resolved_prompt)
    return None


def parse_revision_command(text: str) -> Command | None:
    match = REVISION_COMMAND_PATTERN.match(text.strip())
    if not match:
        return None

    verb = match.group(1).lower()
    remainder = (match.group(2) or "").strip()
    if not remainder:
        return Command(kind="revise", prompt="")

    filename, prompt = extract_requested_filename(remainder)
    prompt = re.sub(r"^(to|and)\s+", "", prompt, flags=re.IGNORECASE).strip()
    if not filename and verb in {"edit", "update"} and not REVISION_TARGET_PATTERN.search(remainder):
        return None
    return Command(kind="revise", filename=filename, prompt=prompt or remainder)


def parse_optional_filename_command(text: str, kind: str) -> Command | None:
    match = re.match(rf"^{kind}\b(?:\s+(.+))?$", text.strip(), re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    remainder = (match.group(1) or "").strip()
    filename = None
    if remainder:
        filename, _ = extract_requested_filename(remainder)
    return Command(kind=kind.lower(), filename=filename)


def looks_like_revision_request(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or looks_like_generation_request(stripped):
        return False
    if parse_revision_command(stripped):
        return True
    if REVISION_START_PATTERN.search(stripped):
        return True
    if re.match(
        r"^\s*(make|turn)\s+(it|this|that|the page|the map|the dashboard|the site|the last one)\b",
        stripped,
        re.IGNORECASE,
    ):
        return True
    return bool(REVISION_TARGET_PATTERN.search(stripped) and REVISION_ACTION_PATTERN.search(stripped))


def parse_command(text: str | None) -> Command | None:
    if not text or not text.strip():
        return None

    stripped = text.strip()
    if stripped.lower() in HELP_ALIASES:
        return Command(kind="help")

    for kind in ("rollback", "history"):
        command = parse_optional_filename_command(stripped, kind)
        if command:
            return command

    revision_command = parse_revision_command(stripped)
    if revision_command:
        return revision_command

    match = re.match(r"^generate\s+(\S+\.html)(?:\s+(.+))?$", stripped, re.IGNORECASE | re.DOTALL)
    if match:
        filename = normalize_html_filename(match.group(1))
        prompt = (match.group(2) or "").strip()
        resolved_prompt = prompt if not is_thin_prompt(prompt) else prompt_from_filename(filename)
        return generation_or_clarification_command(filename, resolved_prompt)

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
        return generation_or_clarification_command(filename_from_prompt(text), text)
    return Command(kind="chat", prompt=text)


def extract_json_object(text: str) -> dict[str, Any]:
    from generation import strip_markdown_fences

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
    question = str(payload.get("question") or "").strip() or None

    if action == "generate":
        return generation_or_clarification_command(filename or filename_from_prompt(prompt), prompt)
    if action == "clarify":
        resolved_filename = filename or filename_from_prompt(prompt)
        return Command(
            kind="clarify",
            filename=resolved_filename,
            prompt=prompt or prompt_from_filename(resolved_filename),
            question=question or clarification_question_for(resolved_filename, prompt),
        )
    if action == "chat":
        return Command(kind="chat", prompt=prompt)

    return fallback_route_message_intent(original_text)


def command_for_source_generation(text: str | None, source_files: list[dict[str, Any]]) -> Command:
    command = parse_command(text)
    if command and command.kind == "help":
        return command
    if command and command.kind in {"generate", "clarify"}:
        return Command(
            kind="generate",
            filename=command.filename or filename_from_source_files(source_files),
            prompt=command.prompt or prompt_from_source_filenames(source_files),
        )

    prompt = command.prompt if command and command.prompt else prompt_from_source_filenames(source_files)
    filename = filename_from_prompt(prompt) if command and command.prompt else filename_from_source_files(source_files)
    return Command(kind="generate", filename=filename, prompt=prompt)


def route_message_intent(anthropic: Any, config: AppConfig, text: str) -> Command:
    from generation import create_anthropic_message, extract_text_content, truncate_text

    hinted_command = local_generation_hint(text)
    if hinted_command:
        return hinted_command

    router_text = truncate_text(text, MAX_ROUTER_INPUT_CHARS)
    request = {
        "model": config.anthropic_model,
        "max_tokens": ROUTER_MAX_TOKENS,
        "system": ROUTER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": router_text}],
    }

    try:
        response = create_anthropic_message(anthropic, request)
        payload = extract_json_object(extract_text_content(response.content))
        return command_from_route_payload(payload, router_text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Router fallback (%s): %s | input=%r",
            type(exc).__name__,
            exc,
            router_text[:120],
        )
        return fallback_route_message_intent(router_text)
    except Exception as exc:  # noqa: BLE001
        if _is_anthropic_api_error(exc):
            logger.warning(
                "Router fallback (%s): %s | input=%r",
                type(exc).__name__,
                exc,
                router_text[:120],
            )
            return fallback_route_message_intent(router_text)
        raise


def _is_anthropic_api_error(exc: BaseException) -> bool:
    try:
        import anthropic

        return isinstance(exc, anthropic.APIError)
    except ImportError:
        return False
