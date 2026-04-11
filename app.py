import html as html_escape
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HELP_TEXT = (
    "*TangoBot usage guide*\n\n"
    "*Beta testing note*\n"
    "TangoBot is in beta testing. Expect rough edges, and review generated pages before sharing them broadly.\n\n"
    "*Chat with Claude*\n"
    "Ask normal questions, brainstorm, summarize, compare options, or work from source material you paste or upload.\n\n"
    "*Generate and host a page*\n"
    "Describe the page you want and I will create a self-contained HTML page, save it, and reply with a Tailscale URL.\n"
    "If the request is too thin, I will ask one clarification question first.\n"
    "Examples:\n"
    "- `make me a marketplace map for enterprise AI with categories and vendor examples`\n"
    "- `build a pricing dashboard for our Q2 packaging options`\n"
    "- `create market-map.html for enterprise AI startups`\n"
    "- `generate market-map.html enterprise AI landscape with categories, companies, and funding`\n"
    "- `generate market-map.html`\n\n"
    "*Use your own source material*\n"
    "Paste notes, company lists, links, or data directly into the DM. "
    "You can also attach `.txt`, `.md`, `.csv`, or `.json` files with a request, and I will use those files as the primary source material.\n\n"
    "*Publish an existing HTML or JSX file*\n"
    "Upload a single `.html` file in this DM and I will publish it directly. "
    "Upload a single self-contained `.jsx` React component with no imports and I will publish both the source and a runnable HTML page.\n\n"
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
DEFAULT_STATE_FILE = Path.home() / ".tangobot" / "pending_clarifications.json"
MAX_ROUTER_INPUT_CHARS = 3000
MAX_MODEL_INPUT_CHARS = 12000
MAX_SOURCE_FILE_CHARS = 12000
MAX_TOTAL_SOURCE_CHARS = 20000
MAX_SLACK_MESSAGE_CHARS = 3500
ROUTER_MAX_TOKENS = 400
CHAT_MAX_TOKENS = 1200
GENERATION_MAX_TOKENS = 4096
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
    state_file: Path = DEFAULT_STATE_FILE


@dataclass(frozen=True)
class Command:
    kind: str
    filename: str | None = None
    prompt: str | None = None
    question: str | None = None


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


def filename_from_prompt(prompt: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", prompt.lower())
        if word not in FILENAME_STOPWORDS
    ]
    return f"{'-'.join(words[:6]).strip('-') or 'page'}.html"


def prompt_from_filename(filename: str) -> str:
    title = title_from_filename(filename)
    return f"Create a complete, useful single-page artifact inferred from the filename: {title}."


def title_from_filename(filename: str | None) -> str:
    if not filename:
        return "page"
    stem = Path(filename).stem
    return re.sub(r"[^A-Za-z0-9]+", " ", stem).strip() or "page"


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


def build_generation_prompt(prompt: str, filename: str | None = None) -> str:
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


def truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n\n[Truncated after {max_chars} characters.]"


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


def write_text_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def load_pending_clarifications(state_file: Path) -> dict[str, dict[str, Any]]:
    if not state_file.exists():
        return {}

    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}
    return {
        str(user_id): state
        for user_id, state in payload.items()
        if isinstance(state, dict)
    }


def save_pending_clarifications(state_file: Path, state: dict[str, dict[str, Any]]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def get_pending_clarification(config: AppConfig, slack_user_id: str) -> dict[str, Any] | None:
    return load_pending_clarifications(config.state_file).get(slack_user_id)


def set_pending_clarification(config: AppConfig, slack_user_id: str, command: Command) -> None:
    state = load_pending_clarifications(config.state_file)
    filename = command.filename or filename_from_prompt(command.prompt or "page")
    prompt = command.prompt or prompt_from_filename(filename)
    state[slack_user_id] = {
        "filename": filename,
        "prompt": prompt,
        "question": command.question or clarification_question_for(filename, prompt),
        "created_at": time.time(),
    }
    save_pending_clarifications(config.state_file, state)


def clear_pending_clarification(config: AppConfig, slack_user_id: str) -> None:
    state = load_pending_clarifications(config.state_file)
    if slack_user_id in state:
        del state[slack_user_id]
        save_pending_clarifications(config.state_file, state)


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
    state_file = Path(os.environ.get("TANGOBOT_STATE_FILE", DEFAULT_STATE_FILE)).expanduser().resolve()

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
        web_search_enabled=env_bool("ANTHROPIC_WEB_SEARCH", False),
        web_search_max_uses=max(env_int("ANTHROPIC_WEB_SEARCH_MAX_USES", 2), 1),
        state_file=state_file,
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


RATE_LIMIT_ERROR_PATTERN = re.compile(
    r"\b(429|rate[_ -]?limit|input tokens per minute|tokens per minute|requests per minute)\b",
    re.IGNORECASE,
)


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
    except Exception:  # noqa: BLE001
        return fallback_route_message_intent(router_text)


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
        "max_tokens": CHAT_MAX_TOKENS,
        "system": chat_system_prompt(config),
        "messages": [{"role": "user", "content": truncate_text(prompt, MAX_MODEL_INPUT_CHARS)}],
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


HTML_REPAIR_PROMPT = (
    "Your previous response could not be published because it was not a complete raw HTML document. "
    "Return a complete, self-contained HTML document now. The first bytes must be <!doctype html>. "
    "Do not include explanations, progress notes, Markdown fences, or any text outside the HTML document."
)


def generate_html(anthropic: Any, config: AppConfig, prompt: str, filename: str | None = None) -> str:
    generation_prompt = build_generation_prompt(prompt, filename)
    messages: list[dict[str, Any]] = [{"role": "user", "content": generation_prompt}]
    request: dict[str, Any] = {
        "model": config.anthropic_model,
        "max_tokens": GENERATION_MAX_TOKENS,
        "system": generation_system_prompt(config),
        "messages": messages,
    }
    tools = web_search_tools(config)
    if tools:
        request["tools"] = tools

    response = create_anthropic_message(anthropic, request)
    search_errors = extract_web_search_errors(response.content)
    if search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(search_errors)}")

    first_sources = extract_cited_sources(response.content)
    try:
        html = extract_html_document(extract_text_content(response.content))
        return append_sources_section(html, first_sources)
    except ValueError as first_error:
        request["messages"] = [
            {
                "role": "user",
                "content": f"{generation_prompt}\n\n{HTML_REPAIR_PROMPT}\n\nValidation error: {first_error}",
            }
        ]

    retry_response = create_anthropic_message(anthropic, request)
    retry_search_errors = extract_web_search_errors(retry_response.content)
    if retry_search_errors:
        raise RuntimeError(f"Anthropic web search failed: {', '.join(retry_search_errors)}")

    try:
        html = extract_html_document(extract_text_content(retry_response.content))
    except ValueError as retry_error:
        raise RuntimeError(f"Anthropic returned invalid HTML after retry: {retry_error}") from retry_error

    sources = merge_sources(first_sources, extract_cited_sources(retry_response.content))
    return append_sources_section(html, sources)


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


def is_supported_html_upload(file_obj: dict[str, Any]) -> bool:
    return Path(file_obj.get("name", "")).suffix.lower() == ".html"


def is_supported_jsx_upload(file_obj: dict[str, Any]) -> bool:
    return Path(file_obj.get("name", "")).suffix.lower() == ".jsx"


def is_supported_upload(file_obj: dict[str, Any]) -> bool:
    return Path(file_obj.get("name", "")).suffix.lower() in ARTIFACT_FILE_SUFFIXES


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
            html_files = [file_obj for file_obj in files if is_supported_html_upload(file_obj)]
            jsx_files = [file_obj for file_obj in files if is_supported_jsx_upload(file_obj)]
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

            for file_obj in jsx_files:
                original_name = file_obj.get("name", "upload.jsx")
                source_name = build_jsx_source_filename(slack_user_id, original_name)
                page_name = build_jsx_page_filename(slack_user_id, original_name)
                source_path = config.sites_dir / source_name
                page_path = config.sites_dir / page_name

                try:
                    jsx_source = download_slack_file(client, config.slack_bot_token, file_obj)
                    page_title = re.sub(r"[^A-Za-z0-9]+", " ", Path(original_name).stem).strip() or "React Page"
                    html = wrap_jsx_as_html(jsx_source, page_title)
                    write_text_file(source_path, jsx_source)
                    write_text_file(page_path, html)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to publish JSX file %s", original_name)
                    say(f"Failed to publish `{original_name}`: {exc}")
                    continue

                say(
                    f"Published `{page_name}`: {publish_url(config, page_name)}\n"
                    f"Source JSX: {publish_url(config, source_name)}"
                )

            if html_files or jsx_files:
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
                    html = generate_html(
                        anthropic,
                        config,
                        build_prompt_with_sources(command.prompt, source_materials),
                        command.filename,
                    )
                    write_text_file(output_path, html)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to generate %s from source attachments", stored_name)
                    say(generation_failure_message(stored_name, exc))
                    return

                say(f"Published `{stored_name}`: {publish_url(config, stored_name)}")
                return

            say(
                "Supported uploads are `.html` or `.jsx` for publishing, "
                "or `.txt`, `.md`, `.csv`, `.json` as generation sources."
            )
            return

        text = event.get("text") or ""
        pending = get_pending_clarification(config, slack_user_id)
        command = parse_command(text)
        if command is None:
            return
        if pending and is_cancel_text(text):
            clear_pending_clarification(config, slack_user_id)
            say("Canceled the pending page request.")
            return
        if command.kind == "help":
            clear_pending_clarification(config, slack_user_id)
            say(HELP_TEXT)
            return
        if pending:
            filename, clarified_prompt = build_prompt_from_clarification(pending, text)
            stored_name = build_site_filename(slack_user_id, filename)
            output_path = config.sites_dir / stored_name
            clear_pending_clarification(config, slack_user_id)

            try:
                say(f"Generating `{stored_name}`...")
                html = generate_html(anthropic, config, clarified_prompt, filename)
                write_text_file(output_path, html)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to generate %s from clarification", stored_name)
                say(generation_failure_message(stored_name, exc))
                return

            say(f"Published `{stored_name}`: {publish_url(config, stored_name)}")
            return
        if command.kind == "route":
            command = route_message_intent(anthropic, config, command.prompt or "")
        if command.kind == "clarify":
            set_pending_clarification(config, slack_user_id, command)
            say(command.question or clarification_question_for(command.filename, command.prompt))
            return
        if command.kind == "chat":
            try:
                say(chat_with_claude(anthropic, config, command.prompt or ""))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to answer chat message")
                say(chat_failure_message(exc))
            return
        assert command.filename is not None
        assert command.prompt is not None

        stored_name = build_site_filename(slack_user_id, command.filename)
        output_path = config.sites_dir / stored_name

        try:
            say(f"Generating `{stored_name}`...")
            html = generate_html(anthropic, config, command.prompt, command.filename)
            write_text_file(output_path, html)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to generate %s", stored_name)
            say(generation_failure_message(stored_name, exc))
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
