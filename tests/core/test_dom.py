import pytest

import wxpath.patches  # noqa: F401  (registers xpath3 + WXPathParser on lxml)
from wxpath.core.dom import (
    get_absolute_links_from_elem_and_xpath,
    get_links_with_anchors_from_elem_and_xpath,
)
from wxpath.core.runtime.helpers import parse_html

# A page where the FIRST <a> has no @href (an in-page target). This is the case
# that defeats naive "re-select //a and zip" pairing: //a yields 4 elements but
# //a/@href yields 3, so positions would shift. Smart-string .getparent() pairs
# each href with its real owner regardless.
HTML = b"""<html><body>
<a name="top">no-href</a>
<a href="/p/1" data-rank="5">one</a>
<a href="/p/2" data-rank="9">two</a>
<nav><a href="/p/3" data-rank="1">three</a></nav>
</body></html>"""

BASE = "https://ex.com/"


@pytest.fixture
def root():
    return parse_html(HTML, base_url=BASE, backlink=None, depth=0, response=None)


class TestAnchorPairedDiscovery:
    def test_urls_are_absolute_and_in_order(self, root):
        pairs = get_links_with_anchors_from_elem_and_xpath(root, "//a/@href")
        assert [u for u, _ in pairs] == [
            "https://ex.com/p/1",
            "https://ex.com/p/2",
            "https://ex.com/p/3",
        ]

    def test_anchor_is_the_owning_element(self, root):
        # The href-less <a name="top"> must NOT shift the pairing.
        pairs = get_links_with_anchors_from_elem_and_xpath(root, "//a/@href")
        ranks = [a.get("data-rank") for _, a in pairs]
        assert ranks == ["5", "9", "1"]

    def test_anchor_is_positioned_in_the_live_tree(self, root):
        # The dom function's job is to return the *right* anchor; how the score
        # expression is evaluated against it lives in ops (_eval_score). Here we
        # only prove the anchor sits in its real place in the document tree, so
        # both attribute (./@data-rank) and structural (./ancestor::nav) scoring
        # have correct context. (lxml getparent walks the live tree directly.)
        pairs = get_links_with_anchors_from_elem_and_xpath(root, "//a/@href")
        assert pairs[0][1].getparent().tag == "body"  # /p/1 anchor under <body>
        assert pairs[2][1].getparent().tag == "nav"   # /p/3 anchor under <nav>

    def test_pairs_align_despite_count_mismatch(self, root):
        # //a (4) != //a/@href (3): the function returns exactly the href links.
        assert len(root.xpath3("//a")) == 4
        pairs = get_links_with_anchors_from_elem_and_xpath(root, "//a/@href")
        assert len(pairs) == 3

    def test_text_selection_pairs_to_owner(self, root):
        # text() also yields smart strings whose .getparent() is the owning <a>.
        pairs = get_links_with_anchors_from_elem_and_xpath(root, "//a/text()")
        assert len(pairs) == 4  # every <a> has text, incl. the href-less one
        assert all(a is not None and a.tag == "a" for _, a in pairs)

    def test_url_set_matches_legacy_discovery(self, root):
        # Standard link selector: anchor-paired URLs == the existing xpath3 path.
        legacy = list(get_absolute_links_from_elem_and_xpath(root, "//a/@href"))
        paired = [u for u, _ in get_links_with_anchors_from_elem_and_xpath(root, "//a/@href")]
        assert paired == legacy
