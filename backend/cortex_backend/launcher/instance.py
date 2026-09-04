"""Per-user instance lock, handoff secret, and record management."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from types import ModuleType
from typing import IO, Any
import uuid

from cortex_backend.core.paths import AppPathError, AppPaths, secure_private_path

msvcrt: ModuleType | None
try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on non-Windows development hosts
    msvcrt = None

fcntl: ModuleType | None
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
    def from_path(cls, path: Path) -> InstanceRecord | None:
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
        self.profile_dir = AppPaths.from_data_dir(profile_dir).data_dir
        self.lock_path = self.profile_dir / "cortex.instance.lock"
        self.record_path = self.profile_dir / "cortex.instance.json"
        self.secret_path = self.profile_dir / "cortex.instance.secret"
        self._handle: IO[bytes] | None = None
        self._record: InstanceRecord | None = None

    def acquire(self, *, port: int) -> InstanceRecord | None:
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            secure_private_path(self.profile_dir, directory=True)
            secret_path = AppPaths.from_data_dir(self.secret_path).data_dir
            if secret_path != self.secret_path:
                return None
        except (OSError, AppPathError):
            return None
        try:
            # Do not use append mode here: every marker write would advance to
            # EOF and make this persistent lock grow once per launch.  Opening
            # without O_TRUNC also preserves the lock file across contenders.
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            handle = os.fdopen(descriptor, "r+b")
        except OSError:
            return None
        try:
            # msvcrt requires a byte to exist before locking the range.  Keep
            # normalization before locking: truncating a Windows file while a
            # byte-range lock is held can invalidate that lock.
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            else:
                handle.seek(0, os.SEEK_END)
                if handle.tell() != 1:
                    handle.truncate(1)
                    handle.flush()
            handle.seek(0)
        except OSError:
            handle.close()
            return None
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
            secure_private_path(self.secret_path, directory=False)
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
        except (OSError, AppPathError):
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

    def read_secret(self, record: InstanceRecord) -> str | None:
        if Path(record.handoff_secret_path) != self.secret_path:
            return None
        try:
            if AppPaths.from_data_dir(self.secret_path).data_dir != self.secret_path:
                return None
            value = Path(record.handoff_secret_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, AppPathError):
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

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
