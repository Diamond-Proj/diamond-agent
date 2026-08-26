"""Federation collector: refreshes the unified resource snapshot every tick.

The collector is source-agnostic: any object with ``probe() -> list[ResourceRecord]``
can feed it (the ResourcePool over the live providers).
It keeps the latest snapshot plus a bounded in-memory history for trend views.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional, Protocol

from ..schemas import ResourceRecord, ResourceSnapshot

logger = logging.getLogger(__name__)


class FederationSource(Protocol):
    def probe(self) -> list[ResourceRecord]: ...


class FederationCollector:
    def __init__(self, source: FederationSource, history_len: int = 240):
        self.source = source
        self._lock = threading.Lock()
        self._latest: Optional[ResourceSnapshot] = None
        self._history: deque[ResourceSnapshot] = deque(maxlen=history_len)

    def refresh(self) -> ResourceSnapshot:
        try:
            records = self.source.probe()
        except Exception:
            logger.exception("federation probe failed; keeping last snapshot")
            if self._latest is not None:
                return self._latest
            raise
        snapshot = ResourceSnapshot(records=records)
        with self._lock:
            self._latest = snapshot
            self._history.append(snapshot)
        return snapshot

    @property
    def latest(self) -> Optional[ResourceSnapshot]:
        with self._lock:
            return self._latest

    def history(self, limit: int = 60) -> list[ResourceSnapshot]:
        with self._lock:
            return list(self._history)[-limit:]
