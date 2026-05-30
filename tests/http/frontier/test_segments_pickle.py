"""Guard the one real M1 risk: CrawlTask.segments must survive pickling.

The SQLite/Redis frontiers persist the remaining expression (`next_segments`,
parser AST nodes) as a pickle blob. If any AST node is unpicklable, durable
backends silently lose work. This test pins the round-trip.
"""

import pickle

import pytest

from wxpath.core import parser
from wxpath.core.models import CrawlIntent
from wxpath.core.runtime.engine import WXPathEngine
from wxpath.http.frontier.memory import InMemoryFrontier


EXPRESSIONS = [
    "url('http://x/')//a/@href",
    "url('http://x/')//url(//a/@href)//h1/text()",
    "url('http://x/')///url(//a/@href)",
    "url('http://x/')///url(//a/@href)/map{ 'u': wx:current-url(), 'links': //a/@href }",
]


@pytest.mark.parametrize("expr", EXPRESSIONS)
def test_parsed_segments_roundtrip(expr):
    """The parsed expression (what a seed CrawlTask carries) pickles cleanly."""
    segments = parser.parse(expr)
    blob = pickle.dumps(segments)
    restored = pickle.loads(blob)
    # Re-pickling the restored object must be byte-stable (no lingering state).
    assert pickle.dumps(restored) == blob


@pytest.mark.asyncio
async def test_crawlintent_next_segments_roundtrip():
    """The next_segments produced for a discovered link (the value actually stored
    in the frontier) pickles cleanly — exercised through the real operator path."""
    from wxpath.core.dom import get_absolute_links_from_elem_and_xpath  # noqa: F401
    from wxpath.core.ops import get_operator
    from wxpath.core.parser import Segments

    # Drive the ///url operator the way the engine does, on a tiny parsed page.
    # base_url is read-only on HtmlElement → set it through the parser.
    from lxml import html as lxml_html
    doc = lxml_html.fromstring(
        "<html><body><a href='http://x/p1'>p1</a></body></html>",
        base_url="http://x/",
    )

    segs = parser.parse("url('http://x/')///url(//a/@href)")
    # segs is [UrlLit, UrlRecurse...]; grab the recurse segment's handler output by
    # feeding the recurse op an element (mirrors _process_pipeline).
    recurse = Segments(list(segs)[1:])
    op = get_operator(recurse[0])
    intents = list(op(doc, recurse, 0))
    crawl_intents = [i for i in intents if isinstance(i, CrawlIntent)]
    assert crawl_intents, "expected at least one CrawlIntent from ///url"
    for ci in crawl_intents:
        blob = pickle.dumps(ci.next_segments)
        assert pickle.loads(blob) is not None
