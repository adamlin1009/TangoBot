# Repository Guidelines

## Project Structure & Module Organization

TangoBot is a small Python Slack bot split across six sibling modules at the repo root. Each module has a single responsibility and no file imports from `app.py`:

- `config.py` — `AppConfig` dataclass, env helpers, `load_config`, input-budget constants, default state/history/versions paths.
- `commands.py` — `Command` dataclass, `parse_command`, `route_message_intent`, clarification heuristics, filename helpers, revision/rollback/history parsers.
- `generation.py` — Anthropic calls (`create_anthropic_message`, `stream_anthropic_message`) with shared rate-limit backoff, `chat_with_claude`, `generate_html`, `revise_published_page`, HTML extraction/repair, JSX helpers, system prompts.
- `storage.py` — atomic `write_text_file`, cross-platform `file_lock` (POSIX `fcntl`, Windows `msvcrt`), clarification state I/O, `cleanup_expired_pages`, page history, version snapshots, rollback.
- `tailscale.py` — `run_tailscale_command`, `detect_tailscale_base_url`, `ensure_tailscale_directory_serving`, `publish_url`.
- `app.py` — Slack wiring only: `create_slack_app`, `handle_message_events`, upload handling, `ThrottledSlackUpdater`, `HELP_TEXT`, `main()`.

Circular edges between `commands` / `generation` / `storage` are broken with lazy imports inside function bodies. `app.py` re-exports `MAX_ROUTER_INPUT_CHARS`, `MAX_MODEL_INPUT_CHARS`, and `MAX_REVISION_HTML_CHARS` so legacy tests keep working.

Tests live in `tests/`, currently focused on helper behavior in `tests/test_helpers.py`. Runtime-published pages are written to a local `sites/` directory, which is created by the app and should not be committed. Environment values are loaded from `.env` in the repository root.

## Build, Test, and Development Commands

- `pip install -r requirements.txt`: install Python dependencies for the bot and tests.
- `python app.py`: run the Slack Socket Mode bot locally. Requires Slack, Anthropic, and Tailscale environment variables.
- `pytest`: run the full test suite from the repo root. Tests import `app` and sibling modules via `importlib.import_module`, so they must run from the root.
- `pytest tests/test_helpers.py -q`: run the helper tests with concise output.
- `pytest tests/test_helpers.py::test_parse_help_command`: run a single test by name.
- `python -c "import config; import commands; import generation; import storage; import tailscale; import app"`: smoke-check that every module imports cleanly with no circular edges after a refactor.

Use a virtual environment when developing locally, for example `python -m venv .venv` followed by `source .venv/bin/activate`.

## Coding Style & Naming Conventions

Follow the style already used across the modules: 4-space indentation, type hints for function signatures where practical, `@dataclass` for small structured records, and clear module-level constants in `UPPER_SNAKE_CASE`. Functions and variables should use `snake_case`; classes should use `PascalCase`. Keep helper functions small and deterministic when possible so they can be tested without Slack, Tailscale, or Anthropic services. Prefer `pathlib.Path` for filesystem paths and structured parsing over ad hoc string handling.

Put new code in the module whose responsibility it fits. Do not import from `app.py`; `app.py` is the Slack wiring layer and depends on everything else. If a new helper needs to live in two modules, prefer a lazy import inside the function body over creating a new import cycle. Route any user text headed for Anthropic through `truncate_text` (in `generation.py`) with an appropriate `MAX_*_CHARS` budget from `config.py`, and write files through `storage.write_text_file` so the atomic `.tmp` + `os.replace` swap is preserved.

## Testing Guidelines

Tests use `pytest`. Add or update tests in `tests/` for parsing, filename normalization, prompt routing, state handling, upload transformations, streaming/backoff behavior, and page history/rollback. Name files `test_*.py` and test functions `test_<behavior>()`. Avoid tests that require live Slack, Anthropic, or Tailscale access; use the existing `_FakeAnthropic`, `_SequenceAnthropic`, `_StopReasonAnthropic`, `_StreamingAnthropic`, and `_RateLimitingAnthropic` fakes in `tests/test_helpers.py` or monkeypatching for external calls. Run `pytest` before opening a pull request.

`tests/test_helpers.py` looks up helpers via `_get_helper(app, "parse_command", ...)`, which tries each name on `app` first and then falls back to the sibling submodules listed in `_SUBMODULES`. When renaming a helper, either preserve the old name or add the new name to the candidate list so the tolerant lookup keeps finding it — otherwise the tests fail with "None of these helpers exist on app or submodules". Streaming tests drive a deterministic clock via `iter([...])` to make tick emission predictable; follow that pattern when adding tests that depend on wall-clock pacing.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Fix incomplete generated HTML retries` and `Support JSX uploads`. Keep commit messages concise, specific, and action-oriented. Pull requests should include a brief description of the behavior change, any relevant setup or environment notes, linked issues if applicable, and test results. Include screenshots or example URLs when changing generated page behavior or Slack-facing responses.

## Security & Configuration Tips

Do not commit `.env`, Slack tokens, Anthropic API keys, generated `sites/` output, or local state files (`pending_clarifications.json`, `page_history.json`, `page_versions/`, or anything under `~/.tangobot/`). Treat uploaded user files and generated HTML as untrusted content; keep validation and filename normalization changes covered by tests.

All paths that write user-controlled content to disk must go through `build_site_filename` in `commands.py` (`{slack_user_id}-{slugify(stem)}.html`) rather than composing paths manually — `slugify` is the only layer defending against path traversal. JSX uploads must pass `validate_jsx_source` (rejects `import` / `require` / non-default `export`) before they are wrapped with `wrap_jsx_as_html`. On Windows hosts, set `SKIP_TAILSCALE_SERVE=1` if Tailscale Serve was pre-configured from an admin shell so normal restarts do not need elevation.
