"""Per-user instance lock, handoff secret, and record management."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from typing import Any
import uuid

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows development hosts
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    pid: int
    port: int
    instance_id: str
    created_at: str
    handoff_secret_path: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_path(cls, path: Path) -> "InstanceRecord | None":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                pid=int(payload["pid"]),
                port=int(payload["port"]),
                instance_id=str(payload["instance_id"]),
                created_at=str(payload["created_at"]),
                handoff_secret_path=str(payload["handoff_secret_path"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


class InstanceLock:
    """Hold an OS-level per-profile lock for the launcher lifetime."""

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self.lock_path = self.profile_dir / "cortex.instance.lock"
        self.record_path = self.profile_dir / "cortex.instance.json"
        self.secret_path = self.profile_dir / "cortex.instance.secret"
        self._handle = None
        self._record: InstanceRecord | None = None

    def acquire(self, *, port: int) -> InstanceRecord | None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            elif fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None

        try:
            secret = secrets.token_urlsafe(32)
            self.secret_path.write_text(secret, encoding="utf-8")
            try:
                os.chmod(self.secret_path, 0o600)
            except OSError:
                pass
            record = InstanceRecord(
                pid=os.getpid(),
                port=port,
                instance_id=uuid.uuid4().hex,
                created_at=datetime.now(timezone.utc).isoformat(),
                handoff_secret_path=str(self.secret_path),
            )
            temporary = self.record_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(record.as_dict(), indent=2), encoding="utf-8"
            )
            os.replace(temporary, self.record_path)
        except OSError:
            handle.close()
            try:
                self.secret_path.unlink()
            except FileNotFoundError:
                pass
            return None
        self._handle = handle
        self._record = record
        return record

    def read_record(self) -> InstanceRecord | None:
        return InstanceRecord.from_path(self.record_path)

    @staticmethod
    def read_secret(record: InstanceRecord) -> str | None:
        try:
            value = Path(record.handoff_secret_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if msvcrt is not None:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            if self._record is not None:
                current = self.read_record()
                if current is not None and current.instance_id == self._record.instance_id:
                    for path in (self.record_path, self.secret_path):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
            self._record = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
