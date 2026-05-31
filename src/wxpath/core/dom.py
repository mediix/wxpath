from urllib.parse import urljoin


def _make_links_absolute(links: list[str], base_url: str) -> list[str]:
    """
    Convert relative links to absolute links based on the base URL.

    Args:
        links (list): List of link strings.
        base_url (str): The base URL to resolve relative links against.

    Returns:
        List of absolute URLs.
    """
    if base_url is None:
        raise ValueError("base_url must not be None when making links absolute.")
    return [urljoin(base_url, link) for link in links if link]


def get_absolute_links_from_elem_and_xpath(elem, xpath):
    base_url = getattr(elem, 'base_url', None)
    return _make_links_absolute(elem.xpath3(xpath), base_url)


def get_links_with_anchors_from_elem_and_xpath(elem, xpath):
    """Discover links *and* their owning anchor element, perfectly aligned.

    Used by M3 dynamic scoring (``priority=<xpath>``), where the score is
    evaluated per link in the context of the element that produced the URL.

    Resolution uses lxml's **native** ``xpath`` (not ``xpath3``): attribute and
    text selections come back as "smart strings" that carry ``.getparent()`` —
    the element owning the matched value. This pairs each URL with its real
    anchor in document order, and crucially avoids the misalignment of
    re-selecting elements separately (e.g. an ``<a name=...>`` with no ``@href``
    appears in ``//a`` but not in ``//a/@href``, which would shift positional
    pairing). Standard link selectors yield the same URL set as ``xpath3``.

    Args:
        elem: The parsed DOM element to query (carries ``base_url``).
        xpath: The link-selecting XPath (e.g. ``//a/@href``).

    Returns:
        A list of ``(absolute_url, anchor_element)`` tuples in document order.
        ``anchor_element`` is ``None`` only if an owner cannot be resolved.
    """
    base_url = getattr(elem, 'base_url', None)
    if base_url is None:
        raise ValueError("base_url must not be None when making links absolute.")

    pairs = []
    for result in elem.xpath(xpath):
        if not result:
            continue
        getparent = getattr(result, 'getparent', None)
        anchor = getparent() if getparent is not None else None
        pairs.append((urljoin(base_url, str(result)), anchor))
    return pairs
