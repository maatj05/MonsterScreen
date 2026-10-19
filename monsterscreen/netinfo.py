"""How to reach the Pi: its name, the IP address per connection (wifi or
cable) and the address of the setup page, shown on screen at startup.

Reads NetworkManager (nmcli), the default on Raspberry Pi OS Bookworm.
"""

import socket
import subprocess
from dataclasses import dataclass, field


@dataclass
class Link:
    kind: str  # "wifi", "ethernet" or "unknown"
    addresses: list[str] = field(default_factory=list)
    ssid: str = ""


def split_terse(line: str) -> list[str]:
    """Splits a line of `nmcli -t` output; colons inside values come escaped as \\:."""
    fields, current, escaped = [], "", False
    for ch in line:
        if escaped:
            current += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append(current)
            current = ""
        else:
            current += ch
    fields.append(current)
    return fields


def _run(*args: str) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def links() -> list[Link]:
    out = _run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device")
    if out is None:
        return _fallback_links()
    result = []
    for line in out.splitlines():
        device, kind, state = (split_terse(line) + ["", ""])[:3]
        if kind not in ("wifi", "ethernet") or not state.startswith("connected"):
            continue
        link = Link(kind)
        # "192.168.1.23/24 | 10.0.0.5/8" when there is more than one.
        ips = _run("nmcli", "-g", "IP4.ADDRESS", "device", "show", device) or ""
        link.addresses = [a.split("/")[0] for a in ips.split() if a != "|"]
        if kind == "wifi":
            # The connection name is often "preconfigured" (Raspberry Pi Imager), so ask for the SSID.
            wifi = _run("nmcli", "-t", "-f", "IN-USE,SSID", "device", "wifi", "list",
                        "ifname", device, "--rescan", "no") or ""
            for row in wifi.splitlines():
                in_use, ssid = (split_terse(row) + [""])[:2]
                if in_use == "*":
                    link.ssid = ssid
        result.append(link)
    return result


def wifi_found() -> bool:
    """False when the Pi has no working wifi chip, as NetworkManager then lists
    no wifi device at all. True when that cannot be told (no NetworkManager)."""
    out = _run("nmcli", "-t", "-f", "TYPE", "device")
    return out is None or "wifi" in out.split()


def _fallback_links() -> list[Link]:
    """Without NetworkManager: only the address of the outgoing interface."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packets are sent for UDP connect
            return [Link("unknown", [s.getsockname()[0]])]
    except OSError:
        return []


def format_summary(hostname: str, mdns: bool, links: list[Link], web_port: int,
                   wifi_found: bool = True) -> str:
    lines = [hostname, ""]
    if not links:
        lines.append("Geen netwerkverbinding")
    for link in links:
        if link.kind == "wifi":
            label = f'Wifi "{link.ssid}"' if link.ssid else "Wifi"
        elif link.kind == "ethernet":
            label = "Kabel (LAN)"
        else:
            label = "Netwerk"
        lines.append(f"{label}: {', '.join(link.addresses) or 'nog geen IP-adres'}")
    if not wifi_found:
        lines.append("Wifi: geen wifi-chip gevonden")
    address = next((a for link in links for a in link.addresses), None)
    if address:
        lines += ["", f"Instellen: http://{address}:{web_port}"]
        if mdns:
            lines.append(f"of http://{hostname}.local:{web_port}")
    return "\n".join(lines)


def summary(web_port: int) -> str:
    mdns = _run("systemctl", "is-active", "--quiet", "avahi-daemon.service") is not None
    return format_summary(socket.gethostname(), mdns, links(), web_port, wifi_found())
