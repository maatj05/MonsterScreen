"""Converts videos the Pi can't play smoothly (WMV) into H.264 MP4.

The Pi 3 has no hardware decoder for WMV, so those files play choppy. After
each sync they are converted once, in the background at low priority, into
a separate folder (the sync would delete anything extra in the media
folder). Until a conversion is done, that video is skipped.
"""

import logging
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

CONVERT_EXTENSIONS = {".wmv", ".asf"}

FFMPEG_ARGS = [
    # Fit within 1920x1080, keep the aspect ratio, and never upscale.
    "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease"
           ":force_divisible_by=2",
    "-fpsmax", "30",
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
    "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart",
]


def needs_conversion(path: Path) -> bool:
    return path.suffix.lower() in CONVERT_EXTENSIONS


class Transcoder:
    def __init__(self, media_dir: Path, out_dir: Path, status=None, ffmpeg: str = "ffmpeg"):
        self.media_dir = media_dir
        self.out_dir = out_dir
        self.status = status
        self.ffmpeg = ffmpeg
        self._failed: set[str] = set()  # targets that failed; retried only when the source changes

    def target_for(self, src: Path) -> Path:
        # Size and mtime in the name: a replaced source gets converted again.
        st = src.stat()
        return self.out_dir / f"{src.name}.{st.st_size}-{int(st.st_mtime)}.mp4"

    def playable(self, src: Path) -> Path | None:
        """The converted file for `src`, or None while it isn't ready."""
        try:
            target = self.target_for(src)
        except FileNotFoundError:
            return None
        return target if target.exists() else None

    def _sources(self) -> list[Path]:
        if not self.media_dir.is_dir():
            return []
        return sorted(p for p in self.media_dir.iterdir()
                      if p.is_file() and not p.name.startswith(".") and needs_conversion(p))

    def pending(self) -> list[Path]:
        result = []
        for src in self._sources():
            try:
                target = self.target_for(src)
            except FileNotFoundError:
                continue
            if not target.exists() and target.name not in self._failed:
                result.append(src)
        return result

    def cleanup(self) -> None:
        """Removes conversions whose source is gone or has changed."""
        if not self.out_dir.is_dir():
            return
        wanted = set()
        for src in self._sources():
            try:
                wanted.add(self.target_for(src).name)
            except FileNotFoundError:
                pass
        for f in self.out_dir.iterdir():
            if f.name not in wanted:
                log.info("Removing old conversion %s", f.name)
                f.unlink(missing_ok=True)

    def convert(self, src: Path) -> bool:
        target = self.target_for(src)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name("." + target.name + ".part.mp4")
        log.info("Converting %s to MP4", src.name)
        # nice: playback always wins the CPU from the conversion.
        cmd = ["nice", "-n", "19", self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *FFMPEG_ARGS, str(tmp)]
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip()[-500:] or f"exit code {result.returncode}")
            tmp.replace(target)
            log.info("Converted %s", src.name)
            return True
        except (OSError, RuntimeError) as e:
            log.error("Could not convert %s: %s", src.name, e)
            self._failed.add(target.name)
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def run_once(self) -> None:
        self.cleanup()
        if self.pending() and not shutil.which(self.ffmpeg):
            log.error("ffmpeg is not installed; WMV videos can't be converted (sudo apt install ffmpeg)")
            if self.status:
                self.status.converting = "ffmpeg ontbreekt (sudo apt install ffmpeg)"
            return
        while pending := self.pending():
            if self.status:
                self.status.converting = f"{pending[0].name} ({len(pending)} te gaan)"
            self.convert(pending[0])
        if self.status:
            self.status.converting = ""


class TranscodeThread(threading.Thread):
    def __init__(self, transcoder: Transcoder):
        super().__init__(daemon=True, name="transcode")
        self.transcoder = transcoder
        self.wake = threading.Event()

    def run(self) -> None:
        while True:
            try:
                self.transcoder.run_once()
            except Exception:
                log.exception("Conversion round failed")
            self.wake.wait(300)
            self.wake.clear()
