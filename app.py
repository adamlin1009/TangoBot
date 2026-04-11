import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from commands import (
    ARTIFACT_FILE_SUFFIXES,
    Command,
    SOURCE_FILE_SUFFIXES,
    build_jsx_page_filename,
    build_jsx_source_filename,
    build_prompt_from_clarification,
    build_site_filename,
    clarification_question_for,
    command_for_source_generation,
    is_cancel_text,
    looks_like_revision_request,
    parse_command,
    route_message_intent,
)
from config import (
    AppConfig,
    MAX_MODEL_INPUT_CHARS,
    MAX_REVISION_HTML_CHARS,
    MAX_ROUTER_INPUT_CHARS,
    load_config,
)
from generation import (
    build_prompt_with_sources,
    chat_failure_message,
    chat_with_claude,
    generate_html,
    generation_failure_message,
    revise_published_page,
    wrap_jsx_as_html,
)
from storage import (
    clear_pending_clarification,
    cleanup_expired_pages,
    get_pending_clarification,
    int_value,
    float_value,
    load_page_history,
    record_page_publish,
    resolve_page_entry,
    rollback_published_page,
    set_pending_clarification,
    write_text_file,
)
from tailscale import ensure_tailscale_directory_serving, publish_url


logger = logging.getLogger(__name__)


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
    "*Revise published pages*\n"
    "After I publish a page, say `revise it to ...`, `add ...`, or `revise filename.html ...` to update the same URL. "
    "Use `rollback` to restore the previous version and `history` to list recent pages.\n\n"
    "*Filenames and access*\n"
    "Generated pages get readable filenames automatically. Uploaded or requested filenames are prefixed with your Slack user ID to avoid collisions. "
    "Anyone on the company tailnet can view the returned link.\n\n"
    "*Useful commands*\n"
    "- `help`\n"
    "- `guide`\n"
    "- `usage`"
)


CLEANUP_INTERVAL_SECONDS = 3600.0
_LAST_CLEANUP_AT = 0.0


class ThrottledSlackUpdater:
    """Edits a single Slack message in place, dropping updates faster than `min_interval`."""

    def __init__(self, client: Any, channel: str, ts: str, min_interval: float = 1.0) -> None:
        self._client = client
        self._channel = channel
        self._ts = ts
        self._min_interval = min_interval
        self._last_update_at = 0.0
        self._last_text: str | None = None

    def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last_update_at < self._min_interval:
            return
        if text == self._last_text:
            return
        self._post(text)
        self._last_update_at = now

    def flush(self, text: str) -> None:
        if text == self._last_text:
            return
        self._post(text)
        self._last_update_at = time.monotonic()

    def _post(self, text: str) -> None:
        try:
            self._client.chat_update(channel=self._channel, ts=self._ts, text=text)
            self._last_text = text
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update Slack message")


def maybe_cleanup_expired_pages(config: AppConfig) -> None:
    global _LAST_CLEANUP_AT
    now = time.monotonic()
    if now - _LAST_CLEANUP_AT < CLEANUP_INTERVAL_SECONDS:
        return
    _LAST_CLEANUP_AT = now
    try:
        cleanup_expired_pages(config.sites_dir, config.page_ttl_days)
    except Exception:  # noqa: BLE001
        logger.exception("Background cleanup sweep failed")


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


def publish_success_message(config: AppConfig, stored_name: str) -> str:
    return (
        f"Published `{stored_name}`: {publish_url(config, stored_name)}\n"
        "To change it, say `revise it to ...`; to undo, say `rollback`."
    )


def format_recent_pages_response(config: AppConfig, slack_user_id: str) -> str:
    history = load_page_history(config.history_file)
    user_state = history.get(slack_user_id)
    pages = user_state.get("pages") if isinstance(user_state, dict) else None
    if not isinstance(pages, dict) or not pages:
        return "No published pages yet."

    recent_pages = sorted(
        [(stored_name, entry) for stored_name, entry in pages.items() if isinstance(entry, dict)],
        key=lambda item: float_value(item[1].get("updated_at") or item[1].get("created_at")),
        reverse=True,
    )
    lines = ["Recent pages:"]
    for stored_name, entry in recent_pages[:8]:
        requested = entry.get("requested_filename") or stored_name
        lines.append(f"- `{requested}` (v{int_value(entry.get('current_version'), 1)}): {publish_url(config, stored_name)}")
    return "\n".join(lines)


def format_page_history_response(config: AppConfig, slack_user_id: str, target_filename: str | None) -> str:
    resolved = resolve_page_entry(config, slack_user_id, target_filename)
    if not resolved:
        return "I could not find that page in your published history."

    stored_name, entry = resolved
    current_version = int_value(entry.get("current_version"))
    versions = entry.get("versions")
    if not isinstance(versions, list) or not versions:
        return f"No saved versions for `{stored_name}` yet."

    lines = [f"History for `{stored_name}` (current v{current_version}):"]
    sorted_versions = sorted(
        [version for version in versions if isinstance(version, dict)],
        key=lambda version: int_value(version.get("version")),
        reverse=True,
    )
    for version in sorted_versions[:8]:
        version_number = int_value(version.get("version"))
        marker = " current" if version_number == current_version else ""
        summary = str(version.get("summary") or version.get("kind") or "published")
        lines.append(f"- v{version_number}{marker}: {summary}")
    return "\n".join(lines)


def command_for_natural_revision(config: AppConfig, slack_user_id: str, text: str) -> Command | None:
    if not resolve_page_entry(config, slack_user_id):
        return None
    if looks_like_revision_request(text):
        return Command(kind="revise", prompt=text.strip())
    return None


def start_updater(say: Any, client: Any, initial_text: str) -> ThrottledSlackUpdater:
    initial = say(initial_text)
    return ThrottledSlackUpdater(client, initial["channel"], initial["ts"])


def create_slack_app(config: AppConfig) -> Any:
    from anthropic import Anthropic
    from slack_bolt import App

    anthropic = Anthropic(api_key=config.anthropic_api_key)
    app = App(token=config.slack_bot_token)

    def run_generation(
        say: Any,
        client: Any,
        slack_user_id: str,
        stored_name: str,
        output_path: Path,
        prompt: str,
        filename: str,
        *,
        initial_text: str,
        publish_kind: str,
        source_filenames: list[str] | None = None,
        prompt_with_sources: str | None = None,
    ) -> None:
        updater = start_updater(say, client, initial_text)
        try:
            html = generate_html(
                anthropic,
                config,
                prompt_with_sources if prompt_with_sources is not None else prompt,
                filename,
                on_progress=updater.update,
            )
            write_text_file(output_path, html)
            record_page_publish(
                config,
                slack_user_id,
                filename,
                stored_name,
                html,
                prompt,
                publish_kind=publish_kind,
                source_filenames=source_filenames,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to generate %s", stored_name)
            updater.flush(generation_failure_message(stored_name, exc))
            return
        updater.flush(publish_success_message(config, stored_name))

    def run_revision(
        say: Any,
        client: Any,
        slack_user_id: str,
        target_filename: str | None,
        instructions: str,
    ) -> None:
        target_label = f"`{target_filename}`" if target_filename else "the last published page"
        updater = start_updater(say, client, f"Revising {target_label}...")
        try:
            entry = revise_published_page(
                anthropic,
                config,
                slack_user_id,
                target_filename,
                instructions,
                on_progress=updater.update,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to revise page")
            updater.flush(f"Revision failed: {exc}")
            return

        stored_name = str(entry.get("stored_name"))
        updater.flush(
            f"Revised `{stored_name}` to v{int_value(entry.get('current_version'))}: "
            f"{publish_url(config, stored_name)}\n"
            "To undo, say `rollback`."
        )

    @app.event("message")
    def handle_message_events(event: dict[str, Any], say: Any, client: Any, logger: Any) -> None:
        if should_ignore_message_event(event):
            return

        slack_user_id = event.get("user")
        if not slack_user_id:
            logger.warning("Skipping DM event without a user ID: %s", event)
            return

        maybe_cleanup_expired_pages(config)

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
                    record_page_publish(
                        config,
                        slack_user_id,
                        original_name,
                        stored_name,
                        html,
                        f"Uploaded HTML file: {original_name}",
                        publish_kind="upload",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to save uploaded file %s", original_name)
                    say(f"Failed to save `{original_name}`: {exc}")
                    continue

                say(publish_success_message(config, stored_name))

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
                    record_page_publish(
                        config,
                        slack_user_id,
                        original_name,
                        page_name,
                        html,
                        f"Uploaded JSX file: {original_name}",
                        publish_kind="jsx upload",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to publish JSX file %s", original_name)
                    say(f"Failed to publish `{original_name}`: {exc}")
                    continue

                say(
                    f"Published `{page_name}`: {publish_url(config, page_name)}\n"
                    f"Source JSX: {publish_url(config, source_name)}\n"
                    "To change the published page, say `revise it to ...`; to undo, say `rollback`."
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

                run_generation(
                    say,
                    client,
                    slack_user_id,
                    stored_name,
                    output_path,
                    command.prompt,
                    command.filename,
                    initial_text=f"Generating `{stored_name}` from attached sources...",
                    publish_kind="source generation",
                    source_filenames=[str(material["name"]) for material in source_materials],
                    prompt_with_sources=build_prompt_with_sources(command.prompt, source_materials),
                )
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
        if command.kind == "history":
            clear_pending_clarification(config, slack_user_id)
            if command.filename:
                say(format_page_history_response(config, slack_user_id, command.filename))
            else:
                say(format_recent_pages_response(config, slack_user_id))
            return
        if command.kind == "rollback":
            clear_pending_clarification(config, slack_user_id)
            try:
                stored_name, entry = rollback_published_page(config, slack_user_id, command.filename)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to roll back page")
                say(f"Rollback failed: {exc}")
                return
            say(
                f"Rolled back `{stored_name}` to v{int_value(entry.get('current_version'))}: "
                f"{publish_url(config, stored_name)}"
            )
            return
        if command.kind == "revise":
            clear_pending_clarification(config, slack_user_id)
            run_revision(say, client, slack_user_id, command.filename, command.prompt or "")
            return
        if pending:
            filename, clarified_prompt = build_prompt_from_clarification(pending, text)
            stored_name = build_site_filename(slack_user_id, filename)
            output_path = config.sites_dir / stored_name
            clear_pending_clarification(config, slack_user_id)
            run_generation(
                say,
                client,
                slack_user_id,
                stored_name,
                output_path,
                clarified_prompt,
                filename,
                initial_text=f"Generating `{stored_name}`...",
                publish_kind="generation",
            )
            return
        if command.kind == "route":
            command = command_for_natural_revision(config, slack_user_id, command.prompt or "") or route_message_intent(
                anthropic,
                config,
                command.prompt or "",
            )
        if command.kind == "revise":
            run_revision(say, client, slack_user_id, command.filename, command.prompt or "")
            return
        if command.kind == "clarify":
            set_pending_clarification(config, slack_user_id, command)
            say(command.question or clarification_question_for(command.filename, command.prompt))
            return
        if command.kind == "chat":
            updater = start_updater(say, client, "Thinking...")
            try:
                text_response = chat_with_claude(
                    anthropic,
                    config,
                    command.prompt or "",
                    on_progress=updater.update,
                )
                updater.flush(text_response)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to answer chat message")
                updater.flush(chat_failure_message(exc))
            return

        assert command.filename is not None
        assert command.prompt is not None
        stored_name = build_site_filename(slack_user_id, command.filename)
        output_path = config.sites_dir / stored_name
        run_generation(
            say,
            client,
            slack_user_id,
            stored_name,
            output_path,
            command.prompt,
            command.filename,
            initial_text=f"Generating `{stored_name}`...",
            publish_kind="generation",
        )

    return app


def main() -> None:
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    config = load_config()
    ensure_tailscale_directory_serving(config)
    cleanup_expired_pages(config.sites_dir, config.page_ttl_days)
    global _LAST_CLEANUP_AT
    _LAST_CLEANUP_AT = time.monotonic()
    app = create_slack_app(config)
    logging.info("Serving %s at %s", config.sites_dir, config.tailscale_base_url)
    SocketModeHandler(app, config.slack_app_token).start()


if __name__ == "__main__":
    main()
