"""Mirrors one Dropbox folder to a local directory.

The local copy is what gets played, so the screen keeps working when the
internet is down. Only changed files are downloaded (compared by Dropbox's
content_hash), and files removed from Dropbox are removed locally.
"""

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.dropboxapi.com"
CONTENT = "https://content.dropboxapi.com"
MANIFEST = ".manifest.json"
FOLDER_MARKER = ".folder"
BLOCK_SIZE = 4 * 1024 * 1024


def content_hash(path: Path) -> str:
    """Dropbox content_hash: sha256 over the sha256 of each 4 MB block."""
    blocks = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(BLOCK_SIZE):
            blocks.update(hashlib.sha256(chunk).digest())
    return blocks.hexdigest()


class DropboxError(Exception):
    pass


def _check(r: requests.Response) -> requests.Response:
    """Like raise_for_status, but keeps Dropbox's own explanation."""
    if r.ok:
        return r
    try:
        body = r.json()
        detail = body.get("error_summary") or body.get("error_description") or body.get("error")
    except ValueError:
        detail = r.text[:200]
    raise DropboxError(f"Dropbox {r.status_code}: {detail}")


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def authorize_url(app_key: str, verifier: str) -> str:
    """Page where the user allows access; Dropbox then shows a code to paste back."""
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": app_key,
        "response_type": "code",
        "token_access_type": "offline",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # No "scope": the link gets whatever the app has ticked under Permissions. Asking
        # for write explicitly would make Dropbox refuse the whole link if it isn't ticked.
    })


def exchange_code(app_key: str, code: str, verifier: str) -> str:
    """Trades the pasted code for a refresh token (PKCE, so no app secret needed)."""
    r = _check(requests.post(f"{API}/oauth2/token", data={
        "code": code.strip(),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
        "client_id": app_key,
    }, timeout=30))
    return r.json()["refresh_token"]


class DropboxClient:
    """Minimal Dropbox API client using a long-lived refresh token."""

    def __init__(self, app_key: str, refresh_token: str, session: requests.Session | None = None):
        self.app_key = app_key
        self.refresh_token = refresh_token
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if self._token is None or time.time() > self._token_expires - 60:
            r = self.session.post(f"{API}/oauth2/token", data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.app_key,
            }, timeout=30)
            body = _check(r).json()
            self._token = body["access_token"]
            self._token_expires = time.time() + body.get("expires_in", 3600)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _list(self, path: str) -> list[dict]:
        r = self.session.post(f"{API}/2/files/list_folder", headers=self._headers(),
                              json={"path": path, "recursive": False}, timeout=30)
        body = _check(r).json()
        entries = body["entries"]
        while body.get("has_more"):
            r = self.session.post(f"{API}/2/files/list_folder/continue", headers=self._headers(),
                                  json={"cursor": body["cursor"]}, timeout=30)
            body = _check(r).json()
            entries += body["entries"]
        return entries

    def list_folder(self, path: str) -> list[dict]:
        return [e for e in self._list(path) if e.get(".tag") == "file"]

    def list_subfolders(self, path: str) -> list[str]:
        return sorted((e["name"] for e in self._list(path) if e.get(".tag") == "folder"), key=str.lower)

    def upload_if_missing(self, path: str, content: bytes) -> bool:
        """Creates a file unless one already exists. Returns True when created."""
        arg = {"path": path, "mode": "add", "autorename": False, "mute": True}
        headers = {**self._headers(), "Dropbox-API-Arg": json.dumps(arg),
                   "Content-Type": "application/octet-stream"}
        r = self.session.post(f"{CONTENT}/2/files/upload", headers=headers, data=content, timeout=60)
        if r.status_code == 409 and "conflict" in r.text:
            return False
        _check(r)
        return True

    def download(self, path: str, dest: Path) -> None:
        # json.dumps escapes non-ASCII, which the Dropbox-API-Arg header requires.
        headers = {**self._headers(), "Dropbox-API-Arg": json.dumps({"path": path})}
        with self.session.post(f"{CONTENT}/2/files/download", headers=headers,
                               stream=True, timeout=60) as r:
            _check(r)
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)


class FolderSync:
    def __init__(self, client, remote_folder: str, local_dir: Path):
        self.client = client
        # Dropbox wants "" for the root, and paths without a trailing slash.
        self.remote_folder = "" if remote_folder.strip("/") == "" else "/" + remote_folder.strip("/")
        self.local_dir = local_dir
        self.manifest_path = local_dir / MANIFEST

    def _read_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _write_manifest(self, manifest: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=1))
        tmp.replace(self.manifest_path)

    def _forget_other_folder(self) -> None:
        """After switching presentations, drop the old one's files at once so
        they don't stay on screen while the new ones download."""
        marker = self.local_dir / FOLDER_MARKER
        try:
            previous = marker.read_text()
        except FileNotFoundError:
            previous = None
        if previous == self.remote_folder.lower():
            return
        if previous is not None:
            log.info("Presentation changed to %s, clearing local copy", self.remote_folder)
            for local in self.local_dir.iterdir():
                if local.is_file():
                    local.unlink()
        marker.write_text(self.remote_folder.lower())

    def _up_to_date(self, entry: dict, local: Path, manifest: dict) -> bool:
        if not local.exists() or local.stat().st_size != entry.get("size"):
            return False
        if manifest.get(entry["name"]) == entry["content_hash"]:
            return True
        # Unknown file (e.g. manifest lost): hash it once instead of re-downloading.
        return content_hash(local) == entry["content_hash"]

    def sync(self) -> bool:
        """Returns True when anything changed locally."""
        self.local_dir.mkdir(parents=True, exist_ok=True)
        entries = self.client.list_folder(self.remote_folder)
        self._forget_other_folder()
        manifest = self._read_manifest()
        changed = False
        remote_names = set()

        for entry in entries:
            name = entry["name"]
            if name.startswith(".") or "/" in name:
                continue
            remote_names.add(name)
            local = self.local_dir / name
            if self._up_to_date(entry, local, manifest):
                manifest[name] = entry["content_hash"]
                continue
            log.info("Downloading %s (%.1f MB)", name, entry.get("size", 0) / 1e6)
            tmp = self.local_dir / f".{name}.part"
            try:
                self.client.download(entry["path_lower"], tmp)
                modified = entry.get("client_modified") or entry.get("server_modified")
                if modified:
                    ts = dt.datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp()
                    os.utime(tmp, (ts, ts))
                tmp.replace(local)  # atomic: the player never sees half a file
            finally:
                tmp.unlink(missing_ok=True)
            manifest[name] = entry["content_hash"]
            changed = True

        for local in self.local_dir.iterdir():
            if local.is_file() and not local.name.startswith(".") and local.name not in remote_names:
                log.info("Removing %s (no longer in Dropbox)", local.name)
                local.unlink()
                changed = True
        manifest = {k: v for k, v in manifest.items() if k in remote_names}
        self._write_manifest(manifest)
        return changed
