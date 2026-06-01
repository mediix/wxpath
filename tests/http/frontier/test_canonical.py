"""M6 layer 1: canonicalize() — pure, idempotent URL normalization."""

import pytest

from wxpath.http.frontier.canonical import DEFAULT_STRIP_PARAMS, canonicalize

STRIP = DEFAULT_STRIP_PARAMS


# --------------------------------------------------------------------------- #
# Structural normalization (always applied)
# --------------------------------------------------------------------------- #
def test_lowercases_scheme_and_host():
    assert canonicalize("HTTP://Example.COM/Path") == "http://example.com/Path"


def test_path_case_is_preserved():
    # Only scheme+host are case-insensitive; the path is significant.
    assert canonicalize("http://h/A/b") == "http://h/A/b"


def test_strips_default_http_port():
    assert canonicalize("http://h:80/x") == "http://h/x"


def test_strips_default_https_port():
    assert canonicalize("https://h:443/x") == "https://h/x"


def test_keeps_nondefault_port():
    assert canonicalize("http://h:8080/x") == "http://h:8080/x"


def test_preserves_userinfo():
    assert canonicalize("http://user:pw@H:80/x") == "http://user:pw@h/x"


# --------------------------------------------------------------------------- #
# Configurable: trailing slash
# --------------------------------------------------------------------------- #
def test_normalizes_trailing_slash():
    assert canonicalize("http://h/p/1/") == "http://h/p/1"


def test_collapses_multiple_trailing_slashes():
    assert canonicalize("http://h/p/1///") == "http://h/p/1"


def test_root_slash_preserved():
    assert canonicalize("http://h/") == "http://h/"


def test_trailing_slash_can_be_disabled():
    assert (
        canonicalize("http://h/p/1/", normalize_trailing_slash=False)
        == "http://h/p/1/"
    )


# --------------------------------------------------------------------------- #
# Configurable: fragment
# --------------------------------------------------------------------------- #
def test_drops_fragment_by_default():
    assert canonicalize("http://h/p#section") == "http://h/p"


def test_fragment_kept_when_disabled():
    assert canonicalize("http://h/p#section", drop_fragment=False) == "http://h/p#section"


# --------------------------------------------------------------------------- #
# Configurable: tracking-param stripping
# --------------------------------------------------------------------------- #
def test_strips_utm_glob():
    assert (
        canonicalize("http://h/p?utm_source=x&utm_campaign=y", strip_params=STRIP)
        == "http://h/p"
    )


def test_strips_named_params():
    assert canonicalize("http://h/p?gclid=abc&fbclid=d", strip_params=STRIP) == "http://h/p"


def test_keeps_semantic_params():
    # A non-tracking param must survive — over-stripping would break real pages.
    assert canonicalize("http://h/p?id=42&utm_source=x", strip_params=STRIP) == "http://h/p?id=42"


def test_param_matching_is_case_insensitive():
    assert canonicalize("http://h/p?UTM_Source=x", strip_params=STRIP) == "http://h/p"


def test_sorts_remaining_params_for_stable_form():
    a = canonicalize("http://h/p?b=2&a=1", strip_params=STRIP)
    b = canonicalize("http://h/p?a=1&b=2", strip_params=STRIP)
    assert a == b == "http://h/p?a=1&b=2"


def test_query_untouched_when_strip_list_empty():
    # No strip list ⇒ no re-encoding, no sorting (conservative passthrough).
    assert canonicalize("http://h/p?b=2&a=1", strip_params=()) == "http://h/p?b=2&a=1"


# --------------------------------------------------------------------------- #
# The acceptance variants all collapse to one canonical form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "variant",
    [
        "http://h/p/1",
        "http://h/p/1?utm_source=x",
        "http://h/p/1#section",
        "http://h/p/1/",
        "HTTP://h:80/p/1/?utm_campaign=y#frag",
    ],
)
def test_acceptance_variants_share_one_canonical_form(variant):
    assert canonicalize(variant, strip_params=STRIP) == "http://h/p/1"


# --------------------------------------------------------------------------- #
# Idempotency + defensiveness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://h/p/1/?utm_source=x#frag",
        "HTTPS://Example.com:443/A/b/?gclid=1&id=2",
        "http://h/",
    ],
)
def test_idempotent(url):
    once = canonicalize(url, strip_params=STRIP)
    twice = canonicalize(once, strip_params=STRIP)
    assert once == twice


@pytest.mark.parametrize("falsy", [None, ""])
def test_falsy_url_returned_unchanged(falsy):
    assert canonicalize(falsy, strip_params=STRIP) == falsy
