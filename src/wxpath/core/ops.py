from typing import Callable, Iterable
from urllib.parse import urljoin

import elementpath
from elementpath.datatypes import AnyAtomicType
from elementpath.xpath3 import XPath3Parser
from lxml import html

from wxpath.core.dom import (
    get_absolute_links_from_elem_and_xpath,
    get_links_with_anchors_from_elem_and_xpath,
)
from wxpath.core.models import (
    CrawlIntent,
    DataIntent,
    ExtractIntent,
    InfiniteCrawlIntent,
    Intent,
    ProcessIntent,
)
from wxpath.core.parser import (
    Binary,
    Call,
    ContextItem,
    Depth,
    Score,
    Segment,
    Segments,
    String,
    Url,
    UrlCrawl,
    Xpath,
)
from wxpath.http.frontier.scoring import needs_anchor_text
from wxpath.patches import WXPathParser
from wxpath.util.logging import get_logger

log = get_logger(__name__)


class WxStr(str):
    """A string with associated base_url and depth metadata for debugging."""
    def __new__(cls, value, base_url=None, depth=-1):
        obj = super().__new__(cls, value)
        obj.base_url = base_url
        obj.depth = depth
        return obj

    def __repr__(self):
        return f"WxStr({super().__repr__()}, base_url={self.base_url!r}, depth={self.depth})"


class RuntimeSetupError(Exception):
    pass


OPS_REGISTER: dict[str, Callable] = {}

def register(func_name_or_type: str | type, args_types: tuple[type, ...] | None = None):
    def _register(func: Callable) -> Callable:
        global OPS_REGISTER
        _key = (func_name_or_type, args_types) if args_types else func_name_or_type
        if _key in OPS_REGISTER:
            raise RuntimeSetupError(f"The operation handler for \"{_key}\" already registered")
        OPS_REGISTER[_key] = func
        return func
    return _register


def get_operator(
        binary_or_segment: Binary | Segment
    ) -> Callable[[html.HtmlElement, list[Url | Xpath], int], Iterable[Intent]]:
    func_name_or_type = getattr(binary_or_segment, 'func', None) or binary_or_segment.__class__

    args_types = None
    if isinstance(binary_or_segment, Binary):
        args_types = (binary_or_segment.left.__class__, binary_or_segment.right.__class__)
    elif isinstance(binary_or_segment, Call):
        args_types = tuple(arg.__class__ for arg in binary_or_segment.args)

    _key = (func_name_or_type, args_types) if args_types else func_name_or_type
    if _key not in OPS_REGISTER:
        raise ValueError(f"Unknown operation: {_key}")
    return OPS_REGISTER[_key]


# ---------------------------------------------------------------------------
# M3 — declarative scoring (priority=)
# ---------------------------------------------------------------------------
def _trailing_score(url_call: Url) -> Score | None:
    """Return the trailing ``Score`` of a url() call, or None if unscored."""
    args = getattr(url_call, "args", None) or []
    return args[-1] if args and isinstance(args[-1], Score) else None


def _eval_score(anchor, expr: str) -> float:
    """Evaluate a dynamic ``priority=`` xpath against a link's anchor element.

    The expression is evaluated with the **whole document** as root but the
    context item positioned at ``anchor`` — so ``.`` is the anchor, ``./@attr``
    reads its attributes, and structural axes like ``./ancestor::article`` walk
    upward correctly (a plain ``anchor.xpath3(expr)`` roots at the anchor and
    would see no ancestors). Uses ``WXPathParser`` so M4's ``wx:`` functions can
    slot in later. A non-numeric / empty result coerces to ``0.0`` (a neutral,
    deterministic priority) rather than aborting the crawl.
    """
    if anchor is None or isinstance(anchor, str):
        return 0.0
    try:
        doc_root = anchor.getroottree().getroot()
        result = elementpath.select(doc_root, expr, item=anchor, parser=WXPathParser)
        if isinstance(result, list):
            result = result[0] if result else 0.0
        return float(result)
    except (TypeError, ValueError, IndexError):
        return 0.0
    except Exception:
        log.exception("priority expression failed", extra={"expr": expr})
        return 0.0


def _resolve_score(score_node: Score | None, anchor) -> float | None:
    """Map a ``Score`` node + anchor to a concrete score (None = unscored)."""
    if score_node is None:
        return None
    if score_node.const is not None:
        return score_node.const
    return _eval_score(anchor, score_node.expr)


def _anchor_text(anchor) -> str | None:
    """Normalized visible text of a link's anchor element (None if unavailable).

    Uses ``itertext()`` (works for both ``HtmlElement`` and elementpath's
    ``XPath3Element``) and collapses whitespace. Captured at discovery for the M5
    semantic scorer; the deterministic scorer ignores it.
    """
    if anchor is None or isinstance(anchor, str):
        return None
    try:
        text = "".join(anchor.itertext())
    except Exception:
        return None
    text = " ".join(text.split())
    return text or None


def _discover_scored(curr_elem, path_exp: str, score_node: Score | None):
    """Yield ``(url, score, anchor_text)`` for each discovered link, deduped (first wins).

    Anchors are resolved — yielding ``anchor_text`` and the node a dynamic score
    needs — when a *dynamic* ``priority=`` is present **or** a text-based scorer is
    active (:func:`needs_anchor_text`, i.e. ``scorer: semantic``). Otherwise the
    existing ``xpath3`` discovery path runs unchanged, with ``anchor_text`` ``None``:
    identical URL set/order to the unscored crawl, so the deterministic default stays
    byte-identical (Invariant I5).
    """
    dynamic = score_node is not None and score_node.const is None
    if dynamic or needs_anchor_text():
        pairs = get_links_with_anchors_from_elem_and_xpath(curr_elem, path_exp)
    else:
        pairs = ((u, None) for u in get_absolute_links_from_elem_and_xpath(curr_elem, path_exp))

    seen = set()
    for url, anchor in pairs:
        if url in seen:
            continue
        seen.add(url)
        yield url, _resolve_score(score_node, anchor), _anchor_text(anchor)


@register('url', (String,))
@register('url', (String, Depth))
@register('url', (String, Xpath))
@register('url', (String, Score))
@register('url', (String, Depth, Xpath))
@register('url', (String, Xpath, Depth))
def _handle_url_str_lit(curr_elem: html.HtmlElement,
                        curr_segments: list[Url | Xpath],
                        curr_depth: int, **kwargs) -> Iterable[Intent]:
    """Handle `url('<literal>')` segments and optional follow xpath."""
    url_call = curr_segments[0] # type: Url

    next_segments = curr_segments[1:]

    # NOTE: Expects parser to produce UrlCrawl node in expressions
    # that look like `url('...', follow=//a/@href)`
    if isinstance(url_call, UrlCrawl):
        xpath_arg = [arg for arg in url_call.args if isinstance(arg, Xpath)][0]
        _segments = [
            UrlCrawl('///url', [xpath_arg, url_call.args[0].value])
        ] + next_segments

        yield CrawlIntent(url=url_call.args[0].value, next_segments=_segments)
    else:
        # A literal seed may carry a constant priority= (a static seed weight).
        score_node = _trailing_score(url_call)
        score = score_node.const if score_node is not None else None
        yield CrawlIntent(
            url=url_call.args[0].value, next_segments=next_segments, score=score
        )


# @register2('url', (Xpath,))
@register(Xpath)
def _handle_xpath(curr_elem: html.HtmlElement,
                  curr_segments: Segments,
                  curr_depth: int,
                  **kwargs) -> Iterable[Intent]:
    """Execute an xpath step and yield data or chained processing intents."""
    xpath_node = curr_segments[0] # type: Xpath

    expr = xpath_node.value

    if curr_elem is None:
        raise ValueError("Element must be provided when path_expr does not start with 'url()'.")
    base_url = getattr(curr_elem, 'base_url', None)
    log.debug("base url", extra={"depth": curr_depth, "op": 'xpath', "base_url": base_url})
    elems = curr_elem.xpath3(expr)
    
    next_segments = curr_segments[1:]
    for elem in elems:
        value_or_elem = WxStr(
            elem, base_url=base_url, 
            depth=curr_depth
        ) if isinstance(elem, str) else elem
        if len(curr_segments) == 1:
            yield DataIntent(value=value_or_elem)
        else:
            yield ProcessIntent(elem=value_or_elem, next_segments=next_segments)


@register('//url', (ContextItem,))
@register('//url', (Xpath,))
@register('//url', (ContextItem, Score))
@register('//url', (Xpath, Score))
@register('/url', (ContextItem,))
@register('/url', (Xpath,))
@register('/url', (ContextItem, Score))
@register('/url', (Xpath, Score))
@register('url', (ContextItem,))
@register('url', (Xpath,))
@register('url', (ContextItem, Score))
@register('url', (Xpath, Score))
def _handle_url_eval(curr_elem: html.HtmlElement | str,
                     curr_segments: list[Url | Xpath],
                     curr_depth: int,
                     **kwargs) -> Iterable[Intent]:
    """Resolve dynamic url() arguments and enqueue crawl intents.

    An optional trailing ``priority=`` (M3) sets ``CrawlIntent.score`` per link.

    Yields:
        CrawlIntent
    """
    url_call = curr_segments[0] # type: Url
    score_node = _trailing_score(url_call)
    next_segments = curr_segments[1:]

    if isinstance(url_call.args[0], ContextItem):
        url = urljoin(getattr(curr_elem, 'base_url', None) or '', curr_elem)
        # `.` may resolve to a bare string (e.g. a prior @href step); only an
        # element carries the context a dynamic score needs.
        anchor = curr_elem if not isinstance(curr_elem, str) else None
        yield CrawlIntent(
            url=url, next_segments=next_segments,
            score=_resolve_score(score_node, anchor),
            anchor_text=_anchor_text(anchor),
        )
        return

    _path_exp = url_call.args[0].value
    # TODO: If prior xpath operation is XPATH_FN_MAP_FRAG, then this will likely fail.
    # It should be handled in the parser.
    for url, score, anchor_text in _discover_scored(curr_elem, _path_exp, score_node):
        # log.debug("queueing", extra={"depth": curr_depth, "op": op, "url": url})
        yield CrawlIntent(url=url, next_segments=next_segments, score=score,
                          anchor_text=anchor_text)


@register('///url', (Xpath,))
@register('///url', (Xpath, Score))
def _handle_url_inf(curr_elem: html.HtmlElement,
                    curr_segments: list[Url | Xpath],
                    curr_depth: int,
                    **kwargs) -> Iterable[CrawlIntent]:
    """Handle the ``///url()`` segment of a wxpath expression.

    This operation is also generated internally by the parser when a
    ``///<xpath>/[/]url()`` segment is encountered.

    Instead of fetching URLs directly, this operator XPaths the current
    element for URLs and queues them for further processing via
    ``_handle_url_inf_and_xpath``. An optional trailing ``priority=`` (M3)
    scores each link and is carried into the recursive re-wrap so every level
    of the infinite crawl keeps scoring.
    """
    url_call = curr_segments[0] # type: Url
    xpath_arg = url_call.args[0]
    score_node = _trailing_score(url_call)

    _path_exp = xpath_arg.value

    tail_segments = curr_segments[1:]
    for url, score, anchor_text in _discover_scored(curr_elem, _path_exp, score_node):
        # Carry the Score forward (before the url) so the next ///url level —
        # reached via _handle_url_inf_and_xpath(args[:-1]) — re-scores.
        inner_args = [xpath_arg, score_node, url] if score_node is not None else [xpath_arg, url]
        _segments = [UrlCrawl('///url', inner_args)] + tail_segments

        yield CrawlIntent(url=url, next_segments=_segments, score=score,
                          anchor_text=anchor_text)


@register('///url', (Xpath, str))
@register('///url', (Xpath, Score, str))
def _handle_url_inf_and_xpath(curr_elem: html.HtmlElement,
                              curr_segments: list[Url | Xpath],
                              curr_depth: int, **kwargs) \
                                -> Iterable[DataIntent | ProcessIntent | InfiniteCrawlIntent]:
    """Handle infinite-crawl with an xpath extraction step.

    This operation is generated internally by the parser; there is no explicit
    wxpath expression that produces it directly.

    Yields:
        DataIntent: If the current element is not None and no next segments are provided.
        ExtractIntent: If the current element is not None and next segments are provided.
        InfiniteCrawlIntent: If the current element is not None and next segments are provided.

    Raises:
        ValueError: If the current element is None.
    """
    url_call = curr_segments[0]

    try:
        if curr_elem is None:
            raise ValueError("Missing element when op is 'url_inf_and_xpath'.")

        next_segments = curr_segments[1:]

        if not next_segments:
            yield DataIntent(value=curr_elem)
        else:
            yield ExtractIntent(elem=curr_elem, next_segments=next_segments)

        # For url_inf, also re-enqueue for further infinite expansion
        _segments = [UrlCrawl('///url', url_call.args[:-1])] + next_segments
        crawl_intent = InfiniteCrawlIntent(elem=curr_elem, next_segments=_segments)

        yield crawl_intent

    except Exception:
        log.exception("error fetching url inf and xpath",
                      extra={"depth": curr_depth, "url": url_call.args[-1]})

@register(Binary, (Xpath, Segments))
def _handle_binary(curr_elem: html.HtmlElement | str, 
                              curr_segments: list[Url | Xpath] | Binary, 
                              curr_depth: int, 
                              **kwargs) -> Iterable[DataIntent | ProcessIntent]:
    """Execute XPath expressions suffixed with the ``!`` (map) operator.

    Yields:
        ProcessIntent: Contrains either a WxStr or lxml or elementpath element.
    """
    left = curr_segments.left
    _ = curr_segments.op
    right = curr_segments.right

    if len(right) == 0:
        # Binary operation on segments expects non-empty segments
        raise ValueError("Binary operation on segments expects non-empty segments")

    base_url = getattr(curr_elem, 'base_url', None)
    next_segments = right

    results = elementpath.select(
        curr_elem,
        left.value,
        parser=XPath3Parser,
        item='' if curr_elem is None else None
    )

    if isinstance(results, AnyAtomicType):
        results = [results]

    for result in results:
        if isinstance(result, str):
            value_or_elem = WxStr(result, base_url=base_url, depth=curr_depth) 
        else:
            value_or_elem = result

        yield ProcessIntent(elem=value_or_elem, next_segments=next_segments)
