"""A comprehensive sample for the chunking test suite.

Exercises every chunk type the PythonChunker emits: module_docstring,
module_preamble, function (sync + async + decorated), class, method
(sync + async + classmethod + staticmethod + property), top_level_block.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable  # noqa: F401

VERSION = "1.0.0"


@lru_cache(maxsize=128)
def cached_thing(key: str) -> int:
    """A decorated top-level function."""
    return hash(key)


async def fetch(url: str) -> bytes:
    """An async top-level function."""
    return b""


class Widget:
    """A class with class-level state and several methods."""

    DEFAULT_SIZE = 42

    def __init__(self, size: int) -> None:
        self.size = size

    @staticmethod
    def make_default() -> "Widget":
        return Widget(Widget.DEFAULT_SIZE)

    @classmethod
    def from_dict(cls, data: dict) -> "Widget":
        return cls(data["size"])

    @property
    def doubled(self) -> int:
        return self.size * 2

    async def render_async(self) -> str:
        return f"<widget size={self.size}>"


if __name__ == "__main__":
    w = Widget.make_default()
    print(w.doubled)
