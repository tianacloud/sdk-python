"""Keep this transport's HPACK header blocks out of dependency debug logs."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging

_private = ContextVar("tiana_private_hpack", default=False)


class _HeaderFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private.get()


for _name in ("hpack.hpack", "hpack.table"):
    logging.getLogger(_name).addFilter(_HeaderFilter())


@contextmanager
def private_headers():
    marker = _private.set(True)
    try:
        yield
    finally:
        _private.reset(marker)
