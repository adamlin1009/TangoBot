import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_STATE_FILE = Path.home() / ".tangobot" / "pending_clarifications.json"
DEFAULT_HISTORY_FILE = Path.home() / ".tangobot" / "page_history.json"
DEFAULT_VERSIONS_DIR = Path.home() / ".tangobot" / "page_versions"
MAX_ROUTER_INPUT_CHARS = 3000
MAX_MODEL_INPUT_CHARS = 12000
MAX_REVISION_HTML_CHARS = 8000
MAX_REVISION_CONTEXT_CHARS = 2000
MAX_SOURCE_FILE_CHARS = 12000
MAX_TOTAL_SOURCE_CHARS = 20000
MAX_SLACK_MESSAGE_CHARS = 3500
ROUTER_MAX_TOKENS = 400
CHAT_MAX_TOKENS = 1200
GENERATION_MAX_TOKENS = 8192


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
    page_ttl_days: int = 90
    state_file: Path = DEFAULT_STATE_FILE
    history_file: Path = DEFAULT_HISTORY_FILE
    versions_dir: Path = DEFAULT_VERSIONS_DIR


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


def load_config() -> AppConfig:
    from tailscale import detect_tailscale_base_url

    load_env_file(Path(".env"))
    sites_dir = Path(os.environ.get("SITES_DIR", Path.home() / "sites")).expanduser().resolve()
    sites_dir.mkdir(parents=True, exist_ok=True)
    state_file = Path(os.environ.get("TANGOBOT_STATE_FILE", DEFAULT_STATE_FILE)).expanduser().resolve()
    history_file = Path(os.environ.get("TANGOBOT_HISTORY_FILE", DEFAULT_HISTORY_FILE)).expanduser().resolve()
    versions_dir = Path(os.environ.get("TANGOBOT_VERSIONS_DIR", DEFAULT_VERSIONS_DIR)).expanduser().resolve()
    versions_dir.mkdir(parents=True, exist_ok=True)

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
        page_ttl_days=env_int("TANGOBOT_PAGE_TTL_DAYS", 90),
        state_file=state_file,
        history_file=history_file,
        versions_dir=versions_dir,
    )
