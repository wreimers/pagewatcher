import pytest

from pagewatcher.html import HtmlNormalizationError, normalize_html


def test_normalizes_body_text_and_whitespace() -> None:
    html = """
        <html>
          <head><title>Ignored title</title></head>
          <body><h1>  Product&nbsp;status </h1><p> In\nstock </p></body>
        </html>
    """

    assert normalize_html(html) == "Product status In stock"


def test_removes_non_content_elements_and_comments_by_default() -> None:
    html = """
        <body>
          Available
          <!-- generated at noon -->
          <script>refreshAt = 123</script>
          <style>.status { color: green; }</style>
          <noscript>Enable JavaScript</noscript>
          <template>Placeholder</template>
        </body>
    """

    assert normalize_html(html) == "Available"


def test_limits_output_to_include_selectors() -> None:
    html = """
        <body>
          <nav>Account Basket</nav>
          <main><h1>Widget</h1><p id="status">Available</p></main>
          <footer>Copyright</footer>
        </body>
    """

    assert normalize_html(html, include_selectors=("main",)) == "Widget Available"


def test_combines_multiple_include_selectors() -> None:
    html = """
        <body>
          <div id="price">$20</div>
          <div id="status">In stock</div>
        </body>
    """

    result = normalize_html(html, include_selectors=("#price", "#status"))

    assert result == "$20 In stock"


def test_does_not_duplicate_overlapping_include_selectors() -> None:
    html = '<main><h1>Widget</h1><p id="status">Available</p></main>'

    result = normalize_html(html, include_selectors=("#status", "main"))

    assert result == "Widget Available"


def test_applies_ignore_selectors_within_included_content() -> None:
    html = """
        <main>
          <p>Only two left</p>
          <span class="timestamp">Updated 12:42:03</span>
        </main>
    """

    result = normalize_html(
        html,
        include_selectors=("main",),
        ignore_selectors=(".timestamp",),
    )

    assert result == "Only two left"


def test_accepts_html_bytes() -> None:
    assert normalize_html(b"<body>Caf\xc3\xa9</body>") == "Caf\N{LATIN SMALL LETTER E WITH ACUTE}"


def test_raises_when_include_selectors_match_nothing() -> None:
    with pytest.raises(
        HtmlNormalizationError, match="include selectors matched no elements"
    ):
        normalize_html("<main>Available</main>", include_selectors=("#missing",))


@pytest.mark.parametrize("purpose", ["include", "ignore"])
def test_wraps_invalid_css_selector_errors(purpose: str) -> None:
    arguments = {f"{purpose}_selectors": ("div[",)}

    with pytest.raises(HtmlNormalizationError, match=f"invalid {purpose} selector"):
        normalize_html("<div>Available</div>", **arguments)


def test_can_preserve_elements_excluded_by_default() -> None:
    html = "<body>Available<noscript>Fallback details</noscript></body>"

    assert normalize_html(html, ignore_selectors=()) == "Available Fallback details"
