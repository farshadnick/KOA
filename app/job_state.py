from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class JobStatus(str, Enum):
    idle = "idle"
    running = "running"
    success = "success"
    failed = "failed"


@dataclass
class JobState:
    job_id: str | None = None
    job_type: str = ""
    status: JobStatus = JobStatus.idle
    phase: str = ""
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    log_lines: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _history_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def history_path(self) -> Path:
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        return data_dir / "job-history.json"

    def reset(self, job_type: str = "job") -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self.job_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
            self.job_type = job_type
            self.status = JobStatus.running
            self.phase = "starting"
            self.message = f"Preparing {job_type} job"
            self.started_at = now.isoformat()
            self.finished_at = None
            self.log_lines = []
            self.stats = {}
        self.log(f"JOB {self.job_id} started ({job_type})")

    def log(self, line: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            self.log_lines.append(f"[{ts}] {line}")
            if len(self.log_lines) > 5000:
                self.log_lines = self.log_lines[-4000:]

    def set_phase(self, phase: str, message: str = "") -> None:
        with self._lock:
            self.phase = phase
            if message:
                self.message = message
        self.log(f"PHASE [{phase}] {message}".rstrip())

    def update_stats(self, **values: Any) -> None:
        with self._lock:
            self.stats.update(values)

    def finish(self, ok: bool, message: str) -> None:
        with self._lock:
            self.status = JobStatus.success if ok else JobStatus.failed
            self.message = message
            self.finished_at = datetime.now(timezone.utc).isoformat()
            if ok:
                self.phase = "done"
        self.log(f"JOB {'completed' if ok else 'failed'}: {message}")
        self._archive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.job_id,
                "type": self.job_type,
                "status": self.status.value,
                "phase": self.phase,
                "message": self.message,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "stats": dict(self.stats),
                "log_tail": self.log_lines[-1000:],
            }

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._history_lock:
            records = self._read_history()
        return records[: max(1, min(limit, 500))]

    def history_job(self, job_id: str) -> dict[str, Any] | None:
        return next(
            (record for record in self.history(500) if record.get("id") == job_id),
            None,
        )

    def _read_history(self) -> list[dict[str, Any]]:
        path = self.history_path
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def _archive(self) -> None:
        record = self.snapshot()
        with self._history_lock:
            records = [
                item
                for item in self._read_history()
                if item.get("id") != record["id"]
            ]
            records.insert(0, record)
            path = self.history_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(records[:100], indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)


job = JobState()
