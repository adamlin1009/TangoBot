import json
import logging
import subprocess

from config import AppConfig, env_bool


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


def ensure_tailscale_directory_serving(config: AppConfig) -> None:
    if env_bool("SKIP_TAILSCALE_SERVE", False):
        logging.info("Skipping tailscale serve because SKIP_TAILSCALE_SERVE is set.")
        return
    run_tailscale_command(config, "serve", "--bg", str(config.sites_dir))


def publish_url(config: AppConfig, stored_name: str) -> str:
    return f"{config.tailscale_base_url}/{stored_name}"
