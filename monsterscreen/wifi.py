"""Wifi networks the Pi knows, managed through NetworkManager (nmcli).

The Pi connects by itself to any known network in range. A default network
(a phone hotspot) is always kept, at the lowest priority, so a screen in a
new place can be reached with a phone and given the local wifi on the setup
page. Changing networks needs the polkit rule that install.sh puts in place.
"""

import logging
import subprocess
import threading
from dataclasses import dataclass

from .netinfo import split_terse

log = logging.getLogger(__name__)

DEFAULT_PRIORITY = -10  # below added networks (0), so the hotspot is only the fallback


class WifiError(Exception):
    pass


def nmcli(*args: str) -> str:
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise WifiError(f"nmcli werkt niet: {e}") from e
    if r.returncode != 0:
        error = r.stderr.strip() or f"nmcli gaf foutcode {r.returncode}"
        if "privileges" in error or "not authorized" in error.lower():
            error = "geen rechten om wifi te wijzigen; draai install.sh opnieuw"
        raise WifiError(error)
    return r.stdout


@dataclass
class Network:
    name: str  # NetworkManager connection name
    ssid: str
    active: bool = False


class Wifi:
    def __init__(self, default_ssid: str = "", default_password: str = "", run=nmcli):
        self.default_ssid = default_ssid
        self.default_password = default_password
        self.run = run

    def saved(self) -> list[Network]:
        networks = []
        for line in self.run("-t", "-f", "NAME,TYPE,ACTIVE", "connection", "show").splitlines():
            name, kind, active = (split_terse(line) + ["", ""])[:3]
            if kind != "802-11-wireless":
                continue
            ssid = split_terse(self.run("-g", "802-11-wireless.ssid", "connection", "show",
                                        "id", name).rstrip("\n"))[0]
            networks.append(Network(name, ssid or name, active == "yes"))
        return sorted(networks, key=lambda n: (not n.active, n.ssid.lower()))

    def visible(self) -> list[str]:
        """Networks in range, strongest first. Uses the last scan so the page stays
        quick, and asks for a fresh one in the background for the next visit."""
        threading.Thread(target=self._rescan, daemon=True).start()
        try:
            out = self.run("-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no")
        except WifiError:
            return []
        names = []
        for line in out.splitlines():
            ssid = split_terse(line)[0]
            if ssid and ssid not in names:  # hidden networks have no name
                names.append(ssid)
        return names

    def _rescan(self) -> None:
        try:
            self.run("device", "wifi", "rescan")
        except WifiError:
            pass  # no wifi device, or scanned very recently

    def add(self, ssid: str, password: str, priority: int = 0) -> None:
        """Saves a network, or updates the password of one already known."""
        if not ssid.strip():
            raise WifiError("Vul de naam van het netwerk in.")
        if password and not 8 <= len(password) <= 63:
            raise WifiError("Een wifi-wachtwoord heeft 8 tot 63 tekens.")
        security = ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password] if password else []
        existing = [n for n in self.saved() if n.ssid == ssid]
        if existing:
            self.run("connection", "modify", "id", existing[0].name,
                     *(security or ["remove", "802-11-wireless-security"]))
        else:
            self.run("connection", "add", "type", "wifi", "con-name", ssid, "ssid", ssid,
                     "connection.autoconnect", "yes",
                     "connection.autoconnect-priority", str(priority), *security)
        log.info("Wifi network %r saved", ssid)

    def remove(self, name: str) -> None:
        network = next((n for n in self.saved() if n.name == name), None)
        if network is None:
            raise WifiError(f"Netwerk '{name}' is niet (meer) bekend.")
        if network.active:
            raise WifiError("Dit netwerk is nu in gebruik; dan zou de Pi onbereikbaar worden.")
        if network.ssid == self.default_ssid:
            raise WifiError("Het standaardnetwerk blijft altijd bewaard.")
        self.run("connection", "delete", "id", name)
        log.info("Wifi network %r removed", network.ssid)

    def ensure_default(self) -> None:
        if not self.default_ssid:
            return
        try:
            if not any(n.ssid == self.default_ssid for n in self.saved()):
                self.add(self.default_ssid, self.default_password, DEFAULT_PRIORITY)
        except WifiError as e:
            log.warning("Could not add default wifi network %r: %s", self.default_ssid, e)
