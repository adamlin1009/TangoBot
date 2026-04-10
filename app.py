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
    "Usage:\n"
    "- Upload one `.html` file in this DM to publish it.\n"
    "- `generate <filename>.html <prompt>` to create a page.\n"
    "- Or just describe the page you want, and I will choose a filename.\n"
    "- `help` to show this message."
)

GENERATION_SYSTEM_PROMPT = (
    "Generate a complete self-contained HTML document. "
    "Return only raw HTML. "
    "Do not wrap the response in Markdown fences. "
    "Inline all CSS and JavaScript."
)


@dataclass(frozen=True)
class AppConfig:
    slack_bot_token: str
    slack_app_token: str
    anthropic_api_key: str
    anthropic_model: str
    sites_dir: Path
    tailscale_bin: str
    tailscale_base_url: str


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


def filename_from_prompt(prompt: str) -> str:
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    return f"{'-'.join(words[:6]).strip('-') or 'page'}.html"


def extract_requested_filename(text: str) -> tuple[str | None, str]:
    match = re.search(r"([A-Za-z0-9][A-Za-z0-9_.-]*\.html)\b", text)
    if not match:
        return None, text.strip()

    filename = normalize_html_filename(match.group(1))
    prompt = re.sub(r"\s+", " ", f"{text[:match.start()]} {text[match.end():]}").strip()
    return filename, prompt or text.strip()


def parse_command(text: str | None) -> Command | None:
    if not text or not text.strip():
        return None

    stripped = text.strip()
    if stripped.lower() == "help":
        return Command(kind="help")

    match = re.match(r"^generate\s+(\S+\.html)\s+(.+)$", stripped, re.IGNORECASE | re.DOTALL)
    if match:
        return Command(
            kind="generate",
            filename=normalize_html_filename(match.group(1)),
            prompt=match.group(2).strip(),
        )

    filename, prompt = extract_requested_filename(stripped)
    return Command(
        kind="generate",
        filename=filename or filename_from_prompt(prompt),
        prompt=prompt,
    )


def extract_text_content(blocks: list[Any]) -> str:
    text_parts: list[str] = []
    for block in blocks:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", ""))
    return "".join(text_parts).strip()


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


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
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
        sites_dir=sites_dir,
        tailscale_bin=tailscale_bin,
        tailscale_base_url=tailscale_base_url.rstrip("/"),
    )


def ensure_tailscale_directory_serving(config: AppConfig) -> None:
    run_tailscale_command(config, "serve", "--bg", str(config.sites_dir))


def publish_url(config: AppConfig, stored_name: str) -> str:
    return f"{config.tailscale_base_url}/{stored_name}"


def generate_html(anthropic: Any, model: str, prompt: str) -> str:
    response = anthropic.messages.create(
        model=model,
        max_tokens=4096,
        system=GENERATION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    html = strip_markdown_fences(extract_text_content(response.content))
    if not html:
        raise RuntimeError("Anthropic returned an empty response.")
    return html


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
            supported_files = [file_obj for file_obj in files if is_supported_upload(file_obj)]
            if not supported_files:
                say("Only `.html` uploads are supported in v1. Send `help` for usage.")
                return

            for file_obj in supported_files:
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
            return

        command = parse_command(event.get("text"))
        if command is None:
            return
        if command.kind == "help":
            say(HELP_TEXT)
            return
        assert command.filename is not None
        assert command.prompt is not None

        stored_name = build_site_filename(slack_user_id, command.filename)
        output_path = config.sites_dir / stored_name

        try:
            say(f"Generating `{stored_name}`...")
            html = generate_html(anthropic, config.anthropic_model, command.prompt)
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
