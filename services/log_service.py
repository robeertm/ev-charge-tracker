"""In-memory log ring buffer + custom handler.

Captures the last N log records so a live log viewer can render them
without reading a file or tailing stdout. Thread-safe.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

RING_SIZE = 2000

_buffer: deque = deque(maxlen=RING_SIZE)
_lock = threading.Lock()
_seq = 0  # monotonic id so the UI can do efficient "give me records > last_id"
_handler: Optional['RingBufferHandler'] = None


class RingBufferHandler(logging.Handler):
    """Logging handler that stores records in a bounded deque."""

    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with _lock:
            _seq += 1
            _buffer.append({
                'id': _seq,
                'ts': record.created,
                'level': record.levelname,
                'logger': record.name,
                'message': msg,
            })


def install(level: int = logging.INFO) -> RingBufferHandler:
    """Attach the ring buffer handler to the root logger (idempotent)."""
    global _handler
    if _handler is not None:
        return _handler
    h = RingBufferHandler()
    h.setLevel(level)
    h.setFormatter(logging.Formatter('%(message)s'))
    logging.getLogger().addHandler(h)
    _handler = h
    return h


def get_entries(after_id: int = 0, level: Optional[str] = None,
                include_requests: bool = True, limit: int = 500):
    """Return log entries with id > after_id.

    - level: only entries with this level or above (optional)
    - include_requests: include werkzeug access logs (if False, filter them out)
    - limit: cap the number of entries returned
    """
    min_level = None
    if level:
        try:
            min_level = logging.getLevelName(level.upper())
            if not isinstance(min_level, int):
                min_level = None
        except Exception:
            min_level = None

    out = []
    with _lock:
        snapshot = list(_buffer)
    for e in snapshot:
        if e['id'] <= after_id:
            continue
        if not include_requests and e['logger'] == 'werkzeug':
            continue
        if min_level is not None:
            try:
                if logging.getLevelName(e['level']) < min_level:
                    continue
            except Exception:
                pass
        out.append(e)
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def clear() -> None:
    """Wipe the ring buffer."""
    with _lock:
        _buffer.clear()


def set_request_logging(enabled: bool) -> None:
    """Toggle werkzeug access-log noise on/off by flipping its level."""
    wlogger = logging.getLogger('werkzeug')
    wlogger.setLevel(logging.INFO if enabled else logging.WARNING)
