"""Switching the connected screen on and off.

Which method works depends on the screen, so it is chosen in settings.toml:
  black     only show black (always works; the screen itself stays on)
  cec       put a TV in standby over HDMI-CEC (needs cec-ctl, package v4l-utils)
  vcgencmd  cut the HDMI signal so a monitor goes to sleep (legacy/fkms driver)
  command   run your own on_command / off_command
"""

import logging
import shlex
import subprocess

log = logging.getLogger(__name__)

METHODS = ("black", "cec", "vcgencmd", "command")

COMMANDS = {
    "black": ("", ""),
    "cec": ("cec-ctl --playback --to 0 --image-view-on", "cec-ctl --playback --to 0 --standby"),
    "vcgencmd": ("vcgencmd display_power 1", "vcgencmd display_power 0"),
}


class ScreenPower:
    def __init__(self, method: str = "black", on_command: str = "", off_command: str = ""):
        if method not in METHODS:
            raise ValueError(f"power.method must be one of {', '.join(METHODS)}")
        if method == "command":
            self.on_command, self.off_command = on_command, off_command
        else:
            self.on_command, self.off_command = COMMANDS[method]
        self.method = method

    def _run(self, command: str) -> None:
        if not command:
            return
        try:
            subprocess.run(shlex.split(command), check=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.SubprocessError) as e:
            log.error("Screen command %r failed: %s", command, e)

    def on(self) -> None:
        log.info("Screen on (%s)", self.method)
        self._run(self.on_command)

    def off(self) -> None:
        log.info("Screen off (%s)", self.method)
        self._run(self.off_command)
