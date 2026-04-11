# TangoBot

Minimal Slack DM bot for publishing single-file HTML pages to a tailnet via Tailscale Serve.

## What v1 does

- Accepts Slack DMs only.
- Supports natural-language generation requests, `generate <filename>.html <prompt>`, and `help`.
- Asks one clarification question for thin page requests before generating.
- Accepts `.html` file uploads and saves them directly.
- Accepts `.jsx` React component uploads and publishes both the source and a runnable `.html` page.
- Accepts `.txt`, `.md`, `.csv`, and `.json` uploads as source material for generation.
- Publishes files from a local `sites/` directory over Tailscale.
- Replies with the tailnet URL for the saved page.

## What v1 does not do

- No `revise` flow.
- No generated JSX; Claude-generated pages are still published as HTML.
- No JSX imports, npm packages, local assets, CSS imports, or multi-file React apps.
- No database, version history, or deployment pipeline.

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

5. In the Anthropic Console, enable web search for the API organization if it is not already enabled.

6. Install Tailscale on the host machine and sign in.

7. Make sure the `tailscale` CLI works on the host machine. The app will run `tailscale serve --bg <sites_dir>` for you on startup. If you want to verify it manually first, use:

```bash
tailscale serve --bg /absolute/path/to/sites
```

## Environment

Copy `.env.example` to `.env` and fill in the values. `TAILSCALE_BASE_URL` is optional; if omitted, the app derives it from `tailscale status --json`.

`ANTHROPIC_WEB_SEARCH=1` is enabled by default so Claude can look up current market data, companies, pricing, and other fresh facts. Web search uses Anthropic's paid search tool in addition to normal token usage. Set `ANTHROPIC_WEB_SEARCH=0` to disable it.

`TANGOBOT_STATE_FILE` is optional. It defaults to `~/.tangobot/pending_clarifications.json` and stores pending one-question clarification flows across bot restarts.

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
what are the current enterprise AI trends?
generate market-map.html enterprise AI landscape with columns for category, company, funding, and stage
generate market-map.html
make me a market map for the enterprise AI landscape
make me a marketplace map for enterprise AI
create market-map.html for the enterprise AI landscape
```

For natural-language requests, the bot picks a readable filename and asks Claude to infer a complete page structure with illustrative content. If a request is too thin, such as only a filename or an artifact type without a subject, the bot asks one clarification question and uses the next DM reply to generate the page. Reply `cancel` to clear a pending clarification. You can also paste source notes, lists, or URLs directly into the message.

If a user uploads an `.html` file in a DM, the bot saves it and returns the tailnet URL. If a user uploads a `.jsx` file, it must be one self-contained React component using global `React`; the bot saves the `.jsx` source and publishes a wrapped `.html` page that loads React, ReactDOM, and Babel from CDNs. If a user uploads `.txt`, `.md`, `.csv`, or `.json` files with a request, the bot uses those files as source material for the generated page.

## Notes

- Filenames are automatically prefixed with the Slack user ID to avoid collisions.
- The host machine must stay online for pages to remain reachable.
- Access is limited to users on the tailnet.
