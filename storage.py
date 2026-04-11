import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from commands import (
    Command,
    build_site_filename,
    clarification_question_for,
    filename_from_prompt,
    normalize_html_filename,
    prompt_from_filename,
)
from config import AppConfig


logger = logging.getLogger(__name__)


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def file_lock(state_file: Path) -> Iterator[None]:
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Windows msvcrt.locking locks a byte range, so the lock file must have
    # at least one byte. Writing a single NUL byte is a harmless no-op on
    # POSIX.
    if not lock_path.exists() or lock_path.stat().st_size == 0:
        lock_path.write_bytes(b"\0")
    with open(lock_path, "r+b") as lock_handle:
        if sys.platform == "win32":
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


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
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_file)


def get_pending_clarification(config: AppConfig, slack_user_id: str) -> dict[str, Any] | None:
    with file_lock(config.state_file):
        return load_pending_clarifications(config.state_file).get(slack_user_id)


def set_pending_clarification(config: AppConfig, slack_user_id: str, command: Command) -> None:
    with file_lock(config.state_file):
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
    with file_lock(config.state_file):
        state = load_pending_clarifications(config.state_file)
        if slack_user_id in state:
            del state[slack_user_id]
            save_pending_clarifications(config.state_file, state)


def cleanup_expired_pages(sites_dir: Path, ttl_days: int) -> int:
    if ttl_days <= 0:
        return 0
    if not sites_dir.exists():
        return 0

    cutoff = time.time() - (ttl_days * 86400)
    deleted = 0
    for entry in sites_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted += 1
        except OSError:
            logger.warning("Failed to delete expired page %s", entry)
    if deleted:
        logger.info("Cleaned up %d pages older than %d days in %s", deleted, ttl_days, sites_dir)
    return deleted


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_page_history(history_file: Path) -> dict[str, dict[str, Any]]:
    if not history_file.exists():
        return {}

    try:
        payload = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    history: dict[str, dict[str, Any]] = {}
    for user_id, user_state in payload.items():
        if not isinstance(user_state, dict):
            continue
        pages = user_state.get("pages")
        if not isinstance(pages, dict):
            pages = {}
        history[str(user_id)] = {
            "last_stored_name": user_state.get("last_stored_name"),
            "pages": {
                str(stored_name): entry
                for stored_name, entry in pages.items()
                if isinstance(entry, dict)
            },
        }
    return history


def save_page_history(history_file: Path, history: dict[str, dict[str, Any]]) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = history_file.with_suffix(history_file.suffix + ".tmp")
    tmp.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, history_file)


def version_snapshot_path(config: AppConfig, stored_name: str, version: int) -> Path:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(stored_name).name)
    stem = Path(safe_name).stem or "page"
    return config.versions_dir / f"{stem}.v{version}.html"


def write_version_snapshot(config: AppConfig, stored_name: str, version: int, html: str) -> Path:
    snapshot_path = version_snapshot_path(config, stored_name, version)
    write_text_file(snapshot_path, html)
    return snapshot_path


def next_page_version(entry: dict[str, Any]) -> int:
    versions = entry.get("versions")
    if not isinstance(versions, list):
        return 1
    version_numbers = [int_value(version.get("version")) for version in versions if isinstance(version, dict)]
    return max(version_numbers, default=0) + 1


def normalize_version_summary(summary: str | None, publish_kind: str) -> str:
    text = re.sub(r"\s+", " ", (summary or "").strip())
    if not text:
        return publish_kind
    if len(text) > 120:
        return f"{text[:117].rstrip()}..."
    return text


def record_page_publish(
    config: AppConfig,
    slack_user_id: str,
    requested_filename: str,
    stored_name: str,
    html: str,
    prompt: str | None,
    *,
    publish_kind: str = "published",
    source_filenames: list[str] | None = None,
) -> dict[str, Any]:
    with file_lock(config.history_file):
        history = load_page_history(config.history_file)
        user_state = history.setdefault(slack_user_id, {"last_stored_name": None, "pages": {}})
        pages = user_state.setdefault("pages", {})
        if not isinstance(pages, dict):
            pages = {}
            user_state["pages"] = pages

        stored_name = Path(stored_name).name
        requested_filename = normalize_html_filename(requested_filename)
        entry = pages.get(stored_name)
        if not isinstance(entry, dict):
            entry = {
                "requested_filename": requested_filename,
                "stored_name": stored_name,
                "original_prompt": prompt or "",
                "versions": [],
                "created_at": time.time(),
            }

        versions = entry.get("versions")
        if not isinstance(versions, list):
            versions = []
        version = next_page_version(entry)
        snapshot_path = write_version_snapshot(config, stored_name, version, html)
        now = time.time()

        versions.append(
            {
                "version": version,
                "path": str(snapshot_path),
                "created_at": now,
                "summary": normalize_version_summary(prompt, publish_kind),
                "kind": publish_kind,
            }
        )
        entry.update(
            {
                "requested_filename": requested_filename,
                "stored_name": stored_name,
                "last_prompt": prompt or entry.get("last_prompt", ""),
                "current_version": version,
                "updated_at": now,
                "versions": versions,
            }
        )
        if not entry.get("original_prompt"):
            entry["original_prompt"] = prompt or ""
        if source_filenames is not None:
            entry["source_filenames"] = list(source_filenames)

        pages[stored_name] = entry
        user_state["last_stored_name"] = stored_name
        save_page_history(config.history_file, history)
        return entry


def resolve_page_entry_from_history(
    history: dict[str, dict[str, Any]],
    slack_user_id: str,
    target_filename: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    user_state = history.get(slack_user_id)
    if not isinstance(user_state, dict):
        return None
    pages = user_state.get("pages")
    if not isinstance(pages, dict) or not pages:
        return None

    if not target_filename:
        last_stored_name = user_state.get("last_stored_name")
        entry = pages.get(last_stored_name) if isinstance(last_stored_name, str) else None
        if isinstance(entry, dict):
            return last_stored_name, entry
        recent_pages = sorted(
            [(stored_name, entry) for stored_name, entry in pages.items() if isinstance(entry, dict)],
            key=lambda item: float_value(item[1].get("updated_at") or item[1].get("created_at")),
            reverse=True,
        )
        if recent_pages:
            return recent_pages[0]
        return None

    raw_name = Path(target_filename).name
    normalized_name = normalize_html_filename(raw_name)
    candidates = {
        raw_name,
        normalized_name,
        build_site_filename(slack_user_id, normalized_name),
    }
    for stored_name, entry in pages.items():
        if not isinstance(entry, dict):
            continue
        if stored_name in candidates:
            return stored_name, entry
        if entry.get("stored_name") in candidates:
            return stored_name, entry
        if entry.get("requested_filename") in candidates:
            return stored_name, entry
    return None


def resolve_page_entry(
    config: AppConfig,
    slack_user_id: str,
    target_filename: str | None = None,
) -> tuple[str, dict[str, Any]] | None:
    return resolve_page_entry_from_history(load_page_history(config.history_file), slack_user_id, target_filename)


def rollback_published_page(
    config: AppConfig,
    slack_user_id: str,
    target_filename: str | None = None,
) -> tuple[str, dict[str, Any]]:
    with file_lock(config.history_file):
        history = load_page_history(config.history_file)
        resolved = resolve_page_entry_from_history(history, slack_user_id, target_filename)
        if not resolved:
            raise LookupError("I could not find a published page to roll back.")

        stored_name, entry = resolved
        current_version = int_value(entry.get("current_version"))
        versions = entry.get("versions")
        if not isinstance(versions, list):
            versions = []
        prior_versions = [
            version
            for version in versions
            if isinstance(version, dict) and 0 < int_value(version.get("version")) < current_version
        ]
        if not prior_versions:
            raise RuntimeError(f"`{stored_name}` does not have an older version to restore.")

        target_version = max(prior_versions, key=lambda version: int_value(version.get("version")))
        snapshot_path = Path(str(target_version.get("path") or ""))
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot for `{stored_name}` v{target_version.get('version')} is missing.")

        write_text_file(config.sites_dir / stored_name, snapshot_path.read_text(encoding="utf-8"))
        entry["current_version"] = int_value(target_version.get("version"))
        entry["last_prompt"] = target_version.get("summary") or entry.get("last_prompt", "")
        entry["updated_at"] = time.time()
        user_state = history.setdefault(slack_user_id, {"last_stored_name": None, "pages": {}})
        user_state["last_stored_name"] = stored_name
        save_page_history(config.history_file, history)
        return stored_name, entry
