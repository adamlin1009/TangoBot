# TangoBot

Minimal Slack DM bot for publishing single-file HTML pages to a tailnet via Tailscale Serve.

TangoBot is in beta testing. Expect rough edges, and review generated pages before sharing them broadly.

## What v1 does

- Accepts Slack DMs only.
- Chats with Claude for general questions, summaries, brainstorming, and analysis — replies stream into the same Slack message as the model generates them.
- Generates self-contained HTML pages from natural-language requests or explicit `generate <filename>.html <prompt>` commands and hosts them on the tailnet.
- Shows elapsed-time ticks (`Generating foo.html — 15s elapsed...`) in place of the "Thinking..." placeholder while a page is being built.
- Asks one clarification question when a page request is too thin, then treats the next DM as the answer. `cancel` clears a pending clarification.
- Accepts uploaded `.html` files and publishes them directly.
- Accepts uploaded `.jsx` React components (single-file, no imports) and publishes both the source and a runnable `.html` page that loads React, ReactDOM, and Babel from CDNs.
- Accepts uploaded `.txt`, `.md`, `.csv`, and `.json` files as source material for the same message's generation request.
- Revises published pages in place from natural-language instructions (`revise it to ...`, `add a ...`, `make it more ...`) while keeping the live URL stable.
- Keeps a private version history for every published page and supports `rollback` and `history` commands.
- Publishes files from a local `sites/` directory over Tailscale Serve and replies with the tailnet URL.
- Retries transient Anthropic rate-limit errors automatically with exponential backoff.
- Sweeps published pages older than `TANGOBOT_PAGE_TTL_DAYS` (default 90; `0` disables) on startup and hourly during normal operation.

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

`ANTHROPIC_GENERATION_MAX_TOKENS` controls the output budget for generated and fully regenerated HTML pages. It defaults to `32768` and is clamped between `8192` and `64000`. Higher values can help very large pages finish, but they also increase latency and rate-limit pressure.

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

TangoBot only listens to direct messages. Start a DM with the bot and say `help` (or `guide` / `usage`) at any time to see the in-bot usage guide. The sections below mirror the commands you will actually use day-to-day.

### Chat with Claude

Ask a question, summarize a document, compare options, or brainstorm — any message that is not a recognized command and does not look like a page-generation request is treated as chat.

```text
what are three ways to pitch our pricing page to a skeptical CFO?
summarize these customer notes into themes and action items
```

Replies stream into the same Slack message as they arrive. You will see the answer grow in place instead of waiting for a single long reply at the end.

### Generate and host a page

Describe the page you want. The bot picks a readable filename, generates a self-contained HTML page, writes it into the `sites/` directory, and replies with the tailnet URL. While it works, the "Thinking..." placeholder is replaced with elapsed-time ticks like `Generating market-map.html — 15s elapsed...` so you know it is still running.

Natural-language requests:

```text
make me a marketplace map for enterprise AI with categories and vendor examples
build a pricing dashboard for our Q2 packaging options
create market-map.html for the enterprise AI landscape with categories and vendor examples
```

Explicit filename and prompt:

```text
generate market-map.html enterprise AI landscape with columns for category, company, funding, and stage
```

Filename only, which triggers a clarification question on the next DM:

```text
generate market-map.html
```

If the bot decides a request is too thin (for example, just a filename, or a broad "market map" without a topic), it will ask one clarification question and use the next DM reply as the answer. Send `cancel` to clear a pending question without generating anything.

### Use your own source material

You can paste notes, company lists, links, or data directly into the DM alongside a page request — the bot will use that text as the primary source for the generated page.

You can also attach one or more `.txt`, `.md`, `.csv`, or `.json` files with the same message. The bot reads the uploaded files, truncates them to a safe size, and feeds them into the generation prompt. Example:

```text
[attach customers.csv and strategy-notes.md]
build a one-page customer health dashboard using these files
```

### Upload existing HTML or JSX files

Drop a single `.html` file into the DM and the bot publishes it directly and returns a URL. No editing, no rewriting.

Drop a single `.jsx` React component and the bot validates it (one file, no `import`/`require`/`export` statements), saves the raw JSX, and publishes a wrapped `.html` page that loads React 18, ReactDOM, and Babel standalone from a CDN and renders your component into `#root`. The component must reference `React` globally rather than importing it.

### Revise a published page in place

After a page is published, send natural-language revision instructions in the same DM thread. The bot updates the same URL, saves the old version privately for rollback, and replies with the same link.

```text
revise it to make the layout cleaner
add a funding stage column
make it more executive
revise market-map.html add a pricing section
```

The unnamed form (`revise it ...`, `add ...`, `make it ...`) targets your most recently published page. The named form (`revise <filename>.html ...`) lets you revise a specific page out of order.

### Rollback and version history

Every publish and revision saves a private snapshot. Old snapshots are stored outside `sites/` so only the current live page is served on the tailnet.

- `rollback` restores the previous version of your most recent page.
- `rollback market-map.html` restores the previous version of a specific page.
- `history` lists your recent pages with version counts and the last update time.

### Cheat sheet

```text
help / guide / usage          → show in-bot usage guide
cancel                        → clear a pending clarification
generate <file>.html <prompt> → explicit page generation
revise <file>.html <prompt>   → revise a specific page
revise it ... / add ...       → revise the most recent page
rollback                      → restore previous version of last page
rollback <file>.html          → restore previous version of a specific page
history                       → list your recent pages and versions
```

## Notes

- Filenames are automatically prefixed with the Slack user ID to avoid collisions.
- Revisions keep the same published URL; old versions are private files used only for rollback. Most revisions are applied as targeted patches first, with full-page regeneration used for broad redesigns or unsafe patches.
- Model inputs are capped before Anthropic calls. Long Slack messages, route-classification input, and uploaded source material are truncated to avoid bloating context and hitting token-per-minute limits.
- The host machine must stay online for pages to remain reachable.
- Access is limited to users on the tailnet.
