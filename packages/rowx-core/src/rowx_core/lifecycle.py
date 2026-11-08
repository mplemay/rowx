import os
import socket
from time import time
from typing import Final

TAG_PREFIX: Final[str] = "rowx:"
ORPHAN_AGE_SECONDS: Final[int] = 3600


def make_tag() -> str:
    return f"{TAG_PREFIX}{os.getpid()}@{socket.gethostname()}:{int(time())}"


def tag_age(comment: str | None, now: float) -> float | None:
    if comment is None or not comment.startswith(TAG_PREFIX):
        return None
    # the host may contain a colon, so the timestamp is whatever follows the last one
    _, _, stamp = comment.rpartition(":")
    try:
        return now - int(stamp)
    except ValueError:
        return None
