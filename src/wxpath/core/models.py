from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass(slots=True)
class CrawlTask:
    """A unit of work for the crawler."""
    elem: Any
    url: str
    segments: List[Tuple[str, str]]
    depth: int
    backlink: Optional[str] = None
    base_url: Optional[str] = None
    
    # Priority for the queue (lower number = higher priority / popped sooner).
    # Defaults to depth (→ BFS); an explicit priority is honored (M2+/M3 scoring).
    priority: Optional[int] = field(default=None)

    def __post_init__(self):
        # Sync priority with depth for BFS behavior ONLY when not explicitly set,
        # so a caller-supplied priority survives (frontier orders by it).
        if self.priority is None:
            self.priority = self.depth

    def __lt__(self, other):
        return self.priority < other.priority

    def __iter__(self):
        return iter((self.elem, self.segments, self.depth, self.backlink))


@dataclass(slots=True)
class Intent:
    pass


@dataclass(slots=True)
class Result(Intent):
    """A container for an extracted item or error."""
    value: Any
    url: str
    depth: int
    error: Optional[Exception] = None
    backlink: Optional[str] = None


@dataclass(slots=True)
class CrawlIntent(Intent):
    url: str             # "I found this link"
    next_segments: list  # "Here is what to do next if you go there"
    # M3: per-link priority from a `priority=` expression (higher = sooner).
    # None means "unscored" → the engine falls back to depth (BFS, Invariant I5).
    # The engine negates this into CrawlTask.priority (lower = popped sooner).
    score: float | None = None
    # provenance: "Provenance | None" = None   # ← M4


@dataclass(slots=True)
class ProcessIntent(Intent):
    elem: Any
    next_segments: list


@dataclass(slots=True)
class InfiniteCrawlIntent(ProcessIntent):
    pass


@dataclass(slots=True)
class ExtractIntent(ProcessIntent):
    """TODO: May be redundant with ProcessIntent?"""
    pass


@dataclass(slots=True)
class CrawlFromAttributeIntent(ProcessIntent):
    pass


@dataclass(slots=True)
class DataIntent(Intent):
    value: Any