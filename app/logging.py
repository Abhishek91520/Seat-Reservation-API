import datetime
import logging
import os
import queue
import sys
import uuid
from collections import deque
from contextvars import ContextVar
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

import structlog

# Context variables for request tracking
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
show_id_var: ContextVar[Optional[str]] = ContextVar("show_id", default=None)
seats_var: ContextVar[Optional[list]] = ContextVar("seats", default=None)
reason_var: ContextVar[Optional[str]] = ContextVar("reason", default=None)


def get_request_id() -> str:
    req_id = request_id_var.get()
    if not req_id:
        req_id = str(uuid.uuid4())
        request_id_var.set(req_id)
    return req_id


_log_queue: queue.Queue = queue.Queue(maxsize=100000)
_queue_listener: Optional[QueueListener] = None


def setup_logging():
    global _queue_listener
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=shared_processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    if _queue_listener is not None:
        try:
            _queue_listener.stop()
        except Exception:
            pass

    _queue_listener = QueueListener(_log_queue, stream_handler, respect_handler_level=True)
    _queue_listener.start()

    # Async worker threads only write into the in-memory queue
    queue_handler = QueueHandler(_log_queue)
    root_logger.handlers = [queue_handler]


# In-memory circular buffer for public log access
recent_logs: deque = deque(maxlen=200)


def record_log_event(event_dict: dict) -> None:
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **event_dict,
    }
    recent_logs.append(entry)


def get_recent_logs(limit: int = 50) -> list:
    logs = list(recent_logs)
    logs.reverse()
    return logs[:limit]

