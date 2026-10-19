"""Drives a single long-running mpv process over its JSON IPC socket.

mpv renders straight to the screen (no desktop needed) and uses the Pi's
hardware video decoder. When nothing is loaded it shows a black screen.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .playlist import Slide

log = logging.getLogger(__name__)

BASE_ARGS = [
    "--idle=yes",
    "--force-window=yes",
    "--fullscreen",
    "--no-terminal",
    "--no-osc",
    "--osd-level=1",  # only our own messages (osd-msg1); no key bindings produce others
    "--osd-font-size=44",
    "--osd-align-x=center",
    "--osd-align-y=center",
    "--no-input-default-bindings",
    "--cursor-autohide=always",
]

# The Pi's zero-copy decoder (v4l2m2m) shows a black picture for videos whose
# width is not a multiple of 64, e.g. 720x576 PAL. Those get the copying variant,
# which is cheap enough at such sizes.
ZERO_COPY_HWDEC = "v4l2m2m"
COPY_HWDEC = "v4l2m2m-copy"
ALIGNMENT = 64


def video_width(path: Path) -> int | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def hwdec_for(width: int | None) -> str:
    return COPY_HWDEC if width and width % ALIGNMENT else ZERO_COPY_HWDEC


class Mpv:
    def __init__(self, extra_args: list[str], socket_path: str):
        self.extra_args = extra_args
        self.socket_path = socket_path
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._buffer = b""
        self._events: list[dict] = []
        self._request_id = 0
        self._message = ""
        self._playing = False
        # Only switch decoders per video when the zero-copy one is configured.
        self._per_video_hwdec = f"--hwdec={ZERO_COPY_HWDEC}" in extra_args
        self._hwdec = ZERO_COPY_HWDEC
        self._widths: dict[tuple[str, float], int | None] = {}

    # --- process management -------------------------------------------------

    def ensure_running(self) -> None:
        if self.process and self.process.poll() is None and self.sock:
            return
        self.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        args = ["mpv", *BASE_ARGS, f"--input-ipc-server={self.socket_path}", *self.extra_args]
        log.info("Starting %s", " ".join(args))
        self.process = subprocess.Popen(args)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"mpv exited with code {self.process.returncode}")
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self.socket_path)
                self.sock = sock
                return
            except (FileNotFoundError, ConnectionRefusedError):
                sock.close()
                time.sleep(0.2)
        raise RuntimeError("mpv IPC socket did not appear")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self._buffer, self._events, self._message, self._playing = b"", [], "", False
        self._hwdec = ZERO_COPY_HWDEC

    # --- IPC ----------------------------------------------------------------

    def _read_message(self, timeout: float) -> dict | None:
        while b"\n" not in self._buffer:
            self.sock.settimeout(max(timeout, 0.01))
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not data:
                raise ConnectionError("mpv closed the IPC connection")
            self._buffer += data
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line) if line.strip() else None

    def command(self, *args):
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        self.sock.sendall(payload.encode())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = self._read_message(deadline - time.monotonic())
            if msg is None:
                continue
            if "event" in msg:
                self._events.append(msg)
            elif msg.get("request_id") == request_id:
                if msg.get("error") != "success":
                    log.warning("mpv %s -> %s", args, msg.get("error"))
                return msg.get("data")
        raise TimeoutError(f"no reply from mpv to {args}")

    def _wait_event(self, names: set[str], timeout: float | None) -> dict | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            while self._events:
                event = self._events.pop(0)
                if event["event"] in names:
                    return event
            remaining = 1.0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return None
            msg = self._read_message(min(remaining, 1.0))
            if msg and "event" in msg:
                self._events.append(msg)

    # --- high level ---------------------------------------------------------

    def apply_settings(self, sound: bool, scaling: str, rotate: int) -> None:
        self.ensure_running()
        self.command("set_property", "mute", not sound)
        self.command("set_property", "panscan", 1.0 if scaling == "fill" else 0.0)
        self.command("set_property", "video-rotate", rotate)

    def play(self, slide: Slide) -> str | None:
        """Shows a slide and blocks until it is done. Returns the end reason."""
        self.ensure_running()
        self.show_message("")
        if slide.kind == "image":
            self.command("set_property", "image-display-duration", slide.duration)
        elif self._per_video_hwdec:
            self._select_hwdec(slide.path)
        self._events.clear()
        self._playing = True
        self.command("loadfile", str(slide.path), "replace")
        # Skip the end-file of whatever was playing before; ours follows start-file.
        if not self._wait_event({"start-file"}, timeout=15):
            log.warning("mpv did not start %s", slide.path.name)
            return None
        if slide.kind == "image":
            timeout = slide.duration + 15
        else:
            timeout = slide.duration if slide.duration > 0 else None
        event = self._wait_event({"end-file"}, timeout)
        if event is None:
            return "timeout"  # video longer than allowed; next loadfile replaces it
        if event.get("reason") == "error":
            log.warning("mpv could not play %s: %s", slide.path.name, event.get("file_error"))
        return event.get("reason")

    def _select_hwdec(self, path: Path) -> None:
        try:
            key = (str(path), path.stat().st_mtime)
        except FileNotFoundError:
            return
        if key not in self._widths:
            self._widths[key] = video_width(path)
        hwdec = hwdec_for(self._widths[key])
        if hwdec != self._hwdec:
            log.info("Using %s for %s (width %s)", hwdec, path.name, self._widths[key])
            self.command("set_property", "hwdec", hwdec)
            self._hwdec = hwdec

    def stop(self) -> None:
        """Clears the screen to black."""
        self.ensure_running()
        self.command("stop")
        self._playing = False
        self.show_message("")

    def show_message(self, text: str) -> None:
        """Shows text on a black screen until replaced or cleared. Repeating the
        same text is cheap and doesn't flicker, so callers can simply keep calling."""
        self.ensure_running()
        if text and self._playing:
            self.command("stop")
            self._playing = False
        if text != self._message:
            self.command("set_property", "osd-msg1", text)
            self._message = text
