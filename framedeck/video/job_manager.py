"""In-process video transcode job registry."""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TranscodeKey:
    media_id: str
    source_mtime: float
    source_size: int
    profile_hash: str

    @classmethod
    def from_profile(
        cls,
        media_id: str,
        source_mtime: float,
        source_size: int,
        profile: dict[str, Any],
    ) -> "TranscodeKey":
        raw = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        return cls(
            media_id=media_id,
            source_mtime=source_mtime,
            source_size=source_size,
            profile_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )


@dataclass
class TranscodeJob:
    key: TranscodeKey
    state: str = "queued"
    progress: float = 0.0
    error: str | None = None


class TranscodeJobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[TranscodeKey, TranscodeJob] = {}

    def get_or_create(self, key: TranscodeKey) -> tuple[TranscodeJob, bool]:
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                return job, False
            job = TranscodeJob(key=key)
            self._jobs[key] = job
            return job, True

    def update(self, key: TranscodeKey, state: str, progress: float | None = None,
               error: str | None = None) -> TranscodeJob:
        with self._lock:
            job = self._jobs[key]
            job.state = state
            if progress is not None:
                job.progress = max(0.0, min(1.0, progress))
            job.error = error
            return job

    def get(self, key: TranscodeKey) -> TranscodeJob | None:
        with self._lock:
            return self._jobs.get(key)
