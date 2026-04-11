# TangoBot

Minimal Slack DM bot for publishing single-file HTML pages to a tailnet via Tailscale Serve.

TangoBot is in beta testing. Expect rough edges, and review generated pages before sharing them broadly.

## What v1 does

- Accepts Slack DMs only.
- Supports natural-language generation requests, `generate <filename>.html <prompt>`, and `help`.
- Asks one clarification question for thin page requests before generating.
- Accepts `.html` file uploads and saves them directly.
- Accepts `.jsx` React component uploads and publishes both the source and a runnable `.html` page.
- Accepts `.txt`, `.md`, `.csv`, and `.json` uploads as source material for generation.
- Revises the last published page or an explicitly named page while keeping the same URL.
- Keeps private version snapshots and supports `rollback` plus `history`.
- Publishes files from a local `sites/` directory over Tailscale.
- Replies with the tailnet URL for the saved page.
- Streams Claude's chat replies into the same Slack message as they arrive and posts elapsed-time ticks while a page generates.
- Sweeps old published pages on startup and hourly using `TANGOBOT_PAGE_TTL_DAYS` (default 90; set to `0` to disable).

## What v1 does not do

- No generated JSX; Claude-generated pages are still published as HTML.
- No JSX imports, npm packages, local assets, CSS imports, or multi-file React apps.
- No database or deployment pipeline.

## Setup

1. Install Python dependencies:

```bash
pip install -r requirements.txt
```

2. Create a Slack app and enable:

- Socket Mode
- Event Subscriptions
- Bot event: `message.im`

3. Add these OAuth scopes to the bot token:

- `chat:write`
- `files:read`
- `im:history`

4. Add an app-level token with:

- `connections:write`

5. Install Tailscale on the host machine and sign in.

6. Make sure the `tailscale` CLI works on the host machine. The app will run `tailscale serve --bg <sites_dir>` for you on startup. If you want to verify it manually first, use:

```bash
tailscale serve --bg /absolute/path/to/sites
```

## Environment

Copy `.env.example` to `.env` and fill in the values. `TAILSCALE_BASE_URL` is optional; if omitted, the app derives it from `tailscale status --json`.

`ANTHROPIC_WEB_SEARCH=0` is the default. This keeps broad page-generation prompts smaller and avoids search/tool context inflating token usage. If you explicitly want live web research, set `ANTHROPIC_WEB_SEARCH=1` and keep `ANTHROPIC_WEB_SEARCH_MAX_USES` low.

`TANGOBOT_STATE_FILE` is optional. It defaults to `~/.tangobot/pending_clarifications.json` and stores pending one-question clarification flows across bot restarts.

`TANGOBOT_HISTORY_FILE` and `TANGOBOT_VERSIONS_DIR` are optional. They default to `~/.tangobot/page_history.json` and `~/.tangobot/page_versions`. Version snapshots are stored outside `sites/`, so only the current live page is served.

`TANGOBOT_PAGE_TTL_DAYS` controls how long published pages live in `sites/` before an automatic sweep deletes them. Defaults to `90`. Set it to `0` to disable the sweep. Cleanup runs once at startup and then at most once per hour while the bot is handling messages.

The default model is `claude-sonnet-4-6`. For deeper reasoning at higher cost, set `ANTHROPIC_MODEL=claude-opus-4-6`.

On Windows, if you already configured Tailscale Serve once from an administrator shell, set `SKIP_TAILSCALE_SERVE=1` so normal bot restarts do not need administrator permissions.

## Run

```bash
python app.py
```

The script reads `.env` from the repo root, ensures the local sites directory exists, configures `tailscale serve`, and then starts the Slack Socket Mode connection.

## Usage

Send the bot a DM in one of these forms:

```text
help
what can you help me build?
summarize these customer notes into themes
generate market-map.html enterprise AI landscape with columns for category, company, funding, and stage
generate market-map.html
revise it to make the layout cleaner
revise market-map.html add a funding stage column
rollback
history
make me a market map for the enterprise AI landscape with buyer categories and vendor examples
make me a marketplace map for enterprise AI with categories and vendor examples
create market-map.html for the enterprise AI landscape with categories and vendor examples
```

For natural-language requests, the bot picks a readable filename and asks Claude to infer a complete page structure with illustrative content. If a request is too thin, such as only a filename, an artifact type without a subject, or a broad market-map request without enough detail, the bot asks one clarification question and uses the next DM reply to generate the page. Reply `cancel` to clear a pending clarification. You can also paste source notes, lists, or URLs directly into the message.

If a user uploads an `.html` file in a DM, the bot saves it and returns the tailnet URL. If a user uploads a `.jsx` file, it must be one self-contained React component using global `React`; the bot saves the `.jsx` source and publishes a wrapped `.html` page that loads React, ReactDOM, and Babel from CDNs. If a user uploads `.txt`, `.md`, `.csv`, or `.json` files with a request, the bot uses those files as source material for the generated page.

After a page is published, reply with natural revision instructions such as `make it more executive`, `add a pricing section`, or `revise market-map.html use a cleaner layout`. Revisions update the same live URL and save private snapshots for rollback. Use `rollback` to restore the previous version of the last page, or `history` to list recent pages and version numbers.

## Notes

- Filenames are automatically prefixed with the Slack user ID to avoid collisions.
- Revisions keep the same published URL; old versions are private files used only for rollback.
- Model inputs are capped before Anthropic calls. Long Slack messages, route-classification input, and uploaded source material are truncated to avoid bloating context and hitting token-per-minute limits.
- The host machine must stay online for pages to remain reachable.
- Access is limited to users on the tailnet.
