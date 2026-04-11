# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install deps: `pip install -r requirements.txt`
- Run the bot: `python app.py` (reads `.env` from repo root, then starts Tailscale Serve + Slack Socket Mode)
- Run all tests: `pytest` (must be run from the repo root — the test suite imports `app` and sibling modules via `importlib.import_module`)
- Run one test: `pytest tests/test_helpers.py::test_parse_help_command`
- Run tests by keyword: `pytest -k generate_html`

There is no linter, formatter, or type-checker configured in the repo. `pytest` is the only quality gate.

## Architecture

Runtime is split across six sibling modules at the repo root. No file may import from `app.py`; `config.py` has no project imports at all. Circular edges between `commands`/`generation`/`storage` are broken with lazy imports inside function bodies.

| Module | Responsibility |
|---|---|
| `config.py` | `AppConfig` dataclass, env helpers, `load_config`, `MAX_*_CHARS` budgets, token caps, default state/history/versions paths, `page_ttl_days`. |
| `commands.py` | `Command` dataclass, `parse_command`, `route_message_intent`, `fallback_route_message_intent`, clarification heuristics, filename helpers (`slugify`, `build_site_filename`, `filename_from_prompt`, `normalize_html_filename`), revision/rollback/history parsers. |
| `generation.py` | Anthropic calls (`create_anthropic_message`, `stream_anthropic_message`), retry/backoff wrapper, `chat_with_claude`, `generate_html`, `build_generation_prompt`, `build_revision_prompt`, `revise_published_page`, HTML extraction/repair, citation merging, JSX helpers, system prompts. |
| `storage.py` | `write_text_file` (atomic), `file_lock` context manager, clarification state I/O, `cleanup_expired_pages`, page history + version snapshots, `resolve_page_entry`, `rollback_published_page`. |
| `tailscale.py` | `run_tailscale_command`, `detect_tailscale_base_url`, `ensure_tailscale_directory_serving`, `publish_url`. |
| `app.py` | Slack wiring only: `create_slack_app`, `handle_message_events`, file upload handling, `ThrottledSlackUpdater`, cleanup throttle, `HELP_TEXT`, `main()`. |

`app.py` re-exports `MAX_ROUTER_INPUT_CHARS`, `MAX_MODEL_INPUT_CHARS`, and `MAX_REVISION_HTML_CHARS` from `config` so legacy tests that reference them via `app.MAX_*` keep working.

### Message dispatch

All inbound traffic is Slack DMs handled by the `@app.event("message")` callback in `create_slack_app` (in `app.py`). Non-DM messages and bot-echo events are filtered by `should_ignore_message_event`. The core abstraction is the frozen `Command` dataclass from `commands.py` with `kind` in `{help, chat, generate, clarify, route, rollback, history, revise}`:

1. `parse_command` is the fast local path: `help` aliases, `rollback`/`history`, explicit `generate <file>.html ...`, natural revision phrasing, and `local_generation_hint`. If none of those match, it returns `kind="route"`.
2. `route_message_intent` runs only when the local path returns `route`. It asks Claude to classify into `chat`/`generate`/`clarify`, parses JSON via `extract_json_object`, and on `(json.JSONDecodeError, ValueError, anthropic.APIError)` falls back to `fallback_route_message_intent`. Unexpected exceptions propagate — don't widen this back to `except Exception`.
3. `generation_or_clarification_command` decides whether a generate request is specific enough to build or needs a clarification question first.

### Clarification state machine

Thin requests stash pending state via `set_pending_clarification` / `get_pending_clarification` / `clear_pending_clarification` (`storage.py`), keyed by Slack user ID in `TANGOBOT_STATE_FILE`. Each call wraps the full read-modify-write in `file_lock(config.state_file)`, which uses `fcntl.flock` on POSIX and `msvcrt.locking` on Windows against a sidecar `.lock` file to serialize concurrent DMs from the same user. On the user's next DM, `build_prompt_from_clarification` merges the original prompt with the answer and generation proceeds. `cancel` clears pending state.

### Streaming and the Anthropic wrapper

`create_anthropic_message` handles `stop_reason == "pause_turn"` by re-sending the message list up to twice. `stream_anthropic_message` wraps `anthropic.messages.stream()` and emits `StreamEvent(kind="delta"|"tick", text_so_far, elapsed_seconds)` via an `on_progress` callback. Ticks fire every `tick_interval` seconds of wall clock; a `clock` parameter (default `time.monotonic`) is injectable for deterministic tests.

Both functions are wrapped by a shared rate-limit backoff helper: max 3 attempts for `create`, 2 for streaming, with delays `(1.0, 3.0, 7.0)` seconds ±30% jitter. Only errors matching `is_rate_limit_error` retry; everything else bubbles on the first failure.

- `chat_with_claude` accepts `on_progress: Callable[[str], None]` and forwards delta text so the Slack message grows in place.
- `generate_html` accepts `on_progress` and forwards only `"tick"` events, rendered by callers as `"Generating `foo.html` — 15s elapsed..."`. The HTML repair retry uses the same stream with a "Repairing" label.
- The router stays on `create_anthropic_message` — ~400 tokens of JSON doesn't need streaming.

### HTML generation and repair

`generate_html` is the only path that calls Claude for page generation. The flow:

1. Build the prompt via `build_generation_prompt` (adds filename hint, truncates to `MAX_MODEL_INPUT_CHARS`).
2. Stream from Anthropic with `GENERATION_MAX_TOKENS` and, optionally, the `web_search_20250305` tool.
3. Parse the response with `extract_html_document`, which strips markdown fences, requires `<!doctype html>` or `<html>` at the start, and validates that `<html>`, `<head>`, `<body>`, and closing `</html>` are all present.
4. On validation failure, retry once with `HTML_REPAIR_PROMPT`. If the first response hit `stop_reason == "max_tokens"`, the retry asks for a compacted version with fewer sections and smaller datasets.
5. Citations from web search (if enabled) are merged across both attempts and appended as a `<section id="sources">` block inserted before `</body>`.

### Page history, revisions, and rollback

`record_page_publish` (`storage.py`) is the single write path for publishing. It takes the file lock on `config.history_file`, appends a versioned snapshot into `config.versions_dir/<stem>.v<N>.html`, and updates `page_history.json` with `current_version`, `last_prompt`, `kind`, and `source_filenames`. `revise_published_page` (`generation.py`) reads the live HTML, builds a `build_revision_prompt` payload truncated to `MAX_REVISION_HTML_CHARS` / `MAX_REVISION_CONTEXT_CHARS`, streams a new version, and calls `record_page_publish` with `publish_kind="revised"`. `rollback_published_page` picks the highest prior version, copies its snapshot back into `sites_dir`, and updates history. Version snapshots live outside `sites_dir` so only the current live page is served over Tailscale.

### Input budgets

Every path that forwards user content to Anthropic is bounded by the `MAX_*_CHARS` constants in `config.py` (`MAX_ROUTER_INPUT_CHARS`, `MAX_MODEL_INPUT_CHARS`, `MAX_SOURCE_FILE_CHARS`, `MAX_TOTAL_SOURCE_CHARS`, `MAX_SLACK_MESSAGE_CHARS`, `MAX_REVISION_HTML_CHARS`, `MAX_REVISION_CONTEXT_CHARS`). `truncate_text` (`generation.py`) enforces the limits and appends a `[Truncated after N characters.]` marker that tests assert on. New Anthropic call sites should route their inputs through `truncate_text` to keep token usage predictable.

### File publishing, cleanup, and atomicity

Uploaded and generated artifacts all land in `config.sites_dir` (created on startup; served by `tailscale serve --bg`). Filenames are built by `build_site_filename` = `{slack_user_id}-{slugify(stem)}.html`. `slugify` strips every character outside `[a-z0-9]` to `-`, which is the only layer defending against path traversal — any new file-writing path should go through these helpers rather than composing paths directly.

All disk writes go through `storage.write_text_file`, which writes to a `.tmp` sidecar and `os.replace`s it into place so Tailscale never serves a half-written page. `cleanup_expired_pages(sites_dir, ttl_days)` prunes files in `sites_dir` older than `TANGOBOT_PAGE_TTL_DAYS` (default 90; `0` disables). `app.main()` runs a sweep on startup, and `maybe_cleanup_expired_pages` in `handle_message_events` re-runs it at most once per hour via a module-level `_LAST_CLEANUP_AT` timestamp.

JSX uploads are a special case: `validate_jsx_source` rejects `import`/`require`/`export` (other than default) and finds a component via `detect_jsx_component_name` regex patterns; `wrap_jsx_as_html` emits an HTML page that pulls React 18 + ReactDOM + Babel standalone from unpkg and renders `<{ComponentName} />` into `#root`. Both the original `.jsx` and the wrapped `.html` are saved. The raw JSX is inlined into a `<script type="text/babel">` block with `</script` neutralized to prevent tag breakout.

### Slack progress updates

`ThrottledSlackUpdater` in `app.py` wraps `(client, channel, ts, min_interval=1.0)` and exposes `update(text)` (silently drops updates faster than `min_interval`) and `flush(text)` (always edits). Both chat and generation pass `updater.update` as the `on_progress` callback; the chat branch drives it with delta text, the generation branch with elapsed-time strings.

### Tailscale integration

`tailscale.ensure_tailscale_directory_serving` runs `tailscale serve --bg <sites_dir>` on startup unless `SKIP_TAILSCALE_SERVE=1` (used on Windows where pre-configured Serve avoids the admin-shell requirement). If `TAILSCALE_BASE_URL` isn't set, `detect_tailscale_base_url` parses `tailscale status --json` and builds `https://<Self.DNSName>`. All subprocess calls use list-form args (`[tailscale_bin, ...]`) — no shell.

### Web search toggle

Off by default (`ANTHROPIC_WEB_SEARCH=0`). When on, `web_search_tools` adds the `web_search_20250305` tool to both chat and generation requests, and the system prompts swap from `*_NO_SEARCH_SYSTEM_PROMPT` (instructing Claude to not claim it browsed) to `*_WEB_SEARCH_SYSTEM_PROMPT` (encouraging citations). Search errors from tool result blocks are surfaced via `extract_web_search_errors` and raised as `RuntimeError`.

## Testing notes

`tests/test_helpers.py` uses a tolerant lookup pattern: `_get_helper(app, "parse_command", "parse_dm_command", "parse_message")` tries multiple function names in order on `app`, then falls back to searching the sibling submodules listed in `_SUBMODULES`. When renaming a helper, either preserve the old name or add the new name to the candidate list in every affected test — otherwise the tests fail with "None of these helpers exist on app or submodules".

Tests mock Anthropic via `_FakeAnthropic`, `_SequenceAnthropic`, `_StopReasonAnthropic`, `_StreamingAnthropic`, and `_RateLimitingAnthropic` (in the test file). They return `SimpleNamespace` objects with `content` and `stop_reason`, so new code that reads more fields off the response must either use `get_block_value` (which tolerates both dicts and attribute-style blocks) or extend the fakes. Streaming tests drive a deterministic clock via `iter([...])` to make tick emission predictable.
