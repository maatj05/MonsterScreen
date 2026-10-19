"""Local settings on the Pi itself (hardware and paths), as opposed to
config.toml in Dropbox which holds everything about the content.

The Dropbox link and the chosen presentation are normally set on the setup
page and saved in the state file; values given here only serve as defaults.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_APP_KEY = "q34alcs3we24vx7"  # public client id of the Dropbox app, not a secret
# v4l2m2m is the Pi's hardware H.264 decoder without copying frames back to RAM; on a
# Pi 3 it played 1080p without dropped frames where "auto-safe" and "-copy" dropped many.
DEFAULT_MPV_ARGS = ["--vo=gpu", "--gpu-context=drm", "--hwdec=v4l2m2m", "--profile=fast"]
# Always known wifi network (a phone hotspot), so a screen in a new place can be reached.
DEFAULT_WIFI_SSID = "Mwg mobiel"
DEFAULT_WIFI_PASSWORD = "Welkom01"


@dataclass
class Settings:
    app_key: str = DEFAULT_APP_KEY
    refresh_token: str = ""
    folder: str = ""
    base_folder: str = "/Mediakranten/Present-it"
    web_port: int = 8080
    wifi_ssid: str = DEFAULT_WIFI_SSID
    wifi_password: str = DEFAULT_WIFI_PASSWORD
    cache_dir: Path = Path.home() / ".cache" / "monsterscreen"
    state_file: Path = Path.home() / ".config" / "monsterscreen" / "state.json"
    mpv_args: list[str] = field(default_factory=lambda: list(DEFAULT_MPV_ARGS))
    power_method: str = "black"
    on_command: str = ""
    off_command: str = ""

    @property
    def media_dir(self) -> Path:
        return self.cache_dir / "media"


def load(path: Path) -> Settings:
    settings = Settings()
    if not path.exists():
        return settings  # everything has a default; the setup page does the rest
    with path.open("rb") as f:
        data = tomllib.load(f)
    dropbox = data.get("dropbox", {})
    settings.app_key = dropbox.get("app_key") or settings.app_key
    settings.refresh_token = dropbox.get("refresh_token", "")
    settings.folder = dropbox.get("folder", "")
    settings.base_folder = dropbox.get("base_folder", settings.base_folder)
    settings.web_port = int(data.get("web", {}).get("port", settings.web_port))
    wifi = data.get("wifi", {})
    settings.wifi_ssid = wifi.get("default_ssid", settings.wifi_ssid)
    settings.wifi_password = wifi.get("default_password", settings.wifi_password)
    paths = data.get("paths", {})
    if "cache_dir" in paths:
        settings.cache_dir = Path(paths["cache_dir"]).expanduser()
    if "state_file" in paths:
        settings.state_file = Path(paths["state_file"]).expanduser()
    settings.mpv_args = data.get("player", {}).get("mpv_args", settings.mpv_args)
    power = data.get("power", {})
    settings.power_method = power.get("method", settings.power_method)
    settings.on_command = power.get("on_command", "")
    settings.off_command = power.get("off_command", "")
    return settings
