"""Persistent HOME_ID manager with subscriber callbacks.

Provides get_or_create_home_id(), read_home_id(), set_home_id(), and a
subscription API so other services can be notified when the HOME_ID changes.
"""
from pathlib import Path
import threading
import secrets
import tempfile
import os
import logging

logger = logging.getLogger(__name__)


class HomeIDManager:
    def __init__(self, data_dir: Path | str | None = None):
        base = Path(__file__).parent.parent
        self.data_dir = Path(data_dir) if data_dir else base / 'data'
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / 'home_id'
        self._lock = threading.RLock()
        self._subscribers = []  # list of callables(new_home_id)

    def read_home_id(self) -> int | None:
        with self._lock:
            if not self._path.exists():
                return None
            try:
                txt = self._path.read_text(encoding='utf-8').strip()
                if not txt:
                    return None
                return int(txt, 0)
            except Exception as e:
                logger.warning(f"Failed to read home_id: {e}")
                return None

    def write_home_id(self, value: int):
        with self._lock:
            tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self.data_dir))
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    f.write(str(int(value)))
                tmp_p = Path(tmp_path)
                try:
                    tmp_p.chmod(0o600)
                except Exception:
                    pass
                tmp_p.replace(self._path)
            finally:
                if Path(tmp_path).exists():
                    try:
                        Path(tmp_path).unlink()
                    except Exception:
                        pass

    def get_or_create_home_id(self) -> int:
        with self._lock:
            existing = self.read_home_id()
            if existing:
                return existing
            # Generate non-zero 32-bit id
            new_id = secrets.randbits(32)
            if new_id == 0:
                new_id = 1
            self.write_home_id(new_id)
            # Notify subscribers
            self._notify_subscribers(new_id)
            return new_id

    def set_home_id(self, new_id: int):
        with self._lock:
            self.write_home_id(new_id)
            self._notify_subscribers(new_id)

    def subscribe(self, callback):
        """Subscribe to HOME_ID changes. Callback called as callback(new_id)."""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _notify_subscribers(self, new_id: int):
        # Call subscribers without holding lock to avoid deadlocks
        subs = []
        with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(new_id)
            except Exception:
                logger.exception("Error in HOME_ID subscriber callback")


# Singleton instance
home_id_manager = HomeIDManager()
