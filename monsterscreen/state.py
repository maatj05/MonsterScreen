"""What the setup page decides: the Dropbox link and the chosen presentation.

Kept apart from settings.toml because the program writes it itself. It holds
the refresh token, so the file is only readable by its owner.
"""

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class State:
    app_key: str = ""
    refresh_token: str = ""
    folder: str = ""  # full Dropbox path of the chosen presentation

    @property
    def linked(self) -> bool:
        return bool(self.app_key and self.refresh_token)

    @property
    def configured(self) -> bool:
        return self.linked and bool(self.folder)


class StateStore:
    def __init__(self, path: Path, defaults: State = State()):
        self.path = path
        self._lock = threading.Lock()
        self._listeners = []
        self.version = 0
        self._state = defaults
        try:
            saved = json.loads(path.read_text())
            known = {k: v for k, v in saved.items() if k in State.__dataclass_fields__ and v}
            self._state = replace(defaults, **known)
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as e:
            log.error("Ignoring unreadable %s: %s", path, e)

    def get(self) -> State:
        with self._lock:
            return self._state

    def update(self, **changes) -> State:
        with self._lock:
            self._state = replace(self._state, **changes)
            self.version += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(self._state), f, indent=1)
            tmp.replace(self.path)
            state, listeners = self._state, list(self._listeners)
        for listener in listeners:
            listener(state)
        return state

    def on_change(self, listener) -> None:
        self._listeners.append(listener)


@dataclass
class SyncStatus:
    """Filled in by the sync thread, shown on the setup page."""

    last_sync: object = None  # datetime of the last successful sync
    last_error: str = ""
    file_count: int = 0
    converting: str = ""  # what the transcoder is working on, empty when idle
