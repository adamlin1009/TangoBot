# TangoBot

Minimal Slack DM bot for publishing single-file HTML pages to a tailnet via Tailscale Serve.

## What v1 does

- Accepts Slack DMs only.
- Supports `generate <filename>.html <prompt>` and `help`.
- Accepts `.html` file uploads and saves them directly.
- Publishes files from a local `sites/` directory over Tailscale.
- Replies with the tailnet URL for the saved page.

## What v1 does not do

- No `revise` flow.
- No `.jsx` support.
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

5. Install Tailscale on the host machine and sign in.

6. Make sure the `tailscale` CLI works on the host machine. The app will run `tailscale serve --bg <sites_dir>` for you on startup. If you want to verify it manually first, use:

```bash
tailscale serve --bg /absolute/path/to/sites
```

## Environment

Copy `.env.example` to `.env` and fill in the values. `TAILSCALE_BASE_URL` is optional; if omitted, the app derives it from `tailscale status --json`.

## Run

```bash
python app.py
```

The script reads `.env` from the repo root, ensures the local sites directory exists, configures `tailscale serve`, and then starts the Slack Socket Mode connection.

## Usage

Send the bot a DM in one of these forms:

```text
help
generate market-map.html enterprise AI landscape with columns for category, company, funding, and stage
```

If a user uploads an `.html` file in a DM, the bot saves it and returns the tailnet URL.

## Notes

- Filenames are automatically prefixed with the Slack user ID to avoid collisions.
- The host machine must stay online for pages to remain reachable.
- Access is limited to users on the tailnet.
