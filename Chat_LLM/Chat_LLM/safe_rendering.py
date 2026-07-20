"""Helpers for rendering untrusted model and user content safely in Qt."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlparse

import markdown


ALLOWED_TAGS = frozenset({
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
})
ALLOWED_ATTRIBUTES = frozenset({"href", "title", "align"})
VOID_TAGS = frozenset({"br", "hr"})
BLOCKED_CONTENT_TAGS = frozenset({"iframe", "math", "object", "script", "style", "svg"})
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def is_safe_external_url(value: str) -> bool:
    """Return whether a link can be opened by the desktop application."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in ALLOWED_URL_SCHEMES and bool(parsed.netloc)


class _SafeHtmlParser(HTMLParser):
    """Keep a small HTML subset and discard active or unsafe attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if self._blocked_depth:
            if tag in BLOCKED_CONTENT_TAGS:
                self._blocked_depth += 1
            return
        if tag in BLOCKED_CONTENT_TAGS:
            self._blocked_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return

        safe_attrs = []
        for name, value in attrs:
            name = name.lower()
            if name not in ALLOWED_ATTRIBUTES or value is None:
                continue
            if name == "href":
                if not is_safe_external_url(value):
                    continue
            elif name == "align" and value.lower() not in {"left", "center", "right"}:
                continue
            safe_attrs.append(f' {name}="{_escape_attribute(value)}"')
        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(self, tag: str, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if self._blocked_depth:
            if tag in BLOCKED_CONTENT_TAGS:
                self._blocked_depth -= 1
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str):
        if not self._blocked_depth:
            self.parts.append(_escape_text(data))

    def handle_entityref(self, name: str):
        if not self._blocked_depth:
            self.parts.append(f"&amp;{name};")

    def handle_charref(self, name: str):
        if not self._blocked_depth:
            self.parts.append(f"&amp;#{name};")

    def handle_comment(self, data: str):
        return


def _escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sanitize_html(value: str) -> str:
    """Sanitize HTML to the allowlist accepted by the chat display."""
    parser = _SafeHtmlParser()
    parser.feed(str(value or ""))
    parser.close()
    return "".join(parser.parts)


def markdown_to_safe_html(value: str, *, fenced_code: bool = False) -> str:
    """Convert Markdown to allowlisted HTML suitable for a rich QLabel."""
    extensions = ["tables", "nl2br"]
    if fenced_code:
        extensions.append("fenced_code")
    return sanitize_html(markdown.markdown(str(value or ""), extensions=extensions))
