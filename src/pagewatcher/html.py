"""Extract stable, meaningful text from HTML documents."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Comment, Tag
from soupsieve import SelectorSyntaxError

DEFAULT_IGNORE_SELECTORS = ("script", "style", "noscript", "template")

_WHITESPACE = re.compile(r"\s+")


class HtmlNormalizationError(ValueError):
    """Raised when configured selectors cannot be used to normalize a page."""


def normalize_html(
    html: str | bytes,
    *,
    include_selectors: Iterable[str] = (),
    ignore_selectors: Iterable[str] = DEFAULT_IGNORE_SELECTORS,
) -> str:
    """Return deterministic text from the selected portions of an HTML document.

    Ignored elements are removed before inclusion selectors are evaluated. When no
    inclusion selectors are supplied, the document body is used if one exists.
    Overlapping inclusion selectors do not duplicate nested content.
    """

    soup = BeautifulSoup(html, "html.parser")
    _remove_comments(soup)

    for selector in tuple(ignore_selectors):
        for element in _select(soup, selector, purpose="ignore"):
            element.decompose()

    included = tuple(include_selectors)
    if included:
        selected = _unique_outermost(
            element
            for selector in included
            for element in _select(soup, selector, purpose="include")
        )
        if not selected:
            selectors = ", ".join(repr(selector) for selector in included)
            raise HtmlNormalizationError(
                f"include selectors matched no elements: {selectors}"
            )
        raw_text = "\n".join(element.get_text(" ", strip=True) for element in selected)
    else:
        root = soup.body if soup.body is not None else soup
        raw_text = root.get_text(" ", strip=True)

    return _WHITESPACE.sub(" ", raw_text).strip()


def _remove_comments(soup: BeautifulSoup) -> None:
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()


def _select(soup: BeautifulSoup, selector: str, *, purpose: str) -> list[Tag]:
    try:
        return soup.select(selector)
    except SelectorSyntaxError as error:
        raise HtmlNormalizationError(
            f"invalid {purpose} selector {selector!r}: {error}"
        ) from error


def _unique_outermost(elements: Iterable[Tag]) -> list[Tag]:
    unique: list[Tag] = []
    seen: set[int] = set()
    for element in elements:
        identity = id(element)
        if identity not in seen:
            unique.append(element)
            seen.add(identity)

    selected_ids = {id(element) for element in unique}
    return [
        element
        for element in unique
        if not any(id(parent) in selected_ids for parent in element.parents)
    ]
