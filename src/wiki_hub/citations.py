"""Small local-evidence citation convention, without fetching external links."""

import posixpath
import re
from collections import Counter
from urllib.parse import unquote, urlsplit

from .gitstore import ContentError, content_path

LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")
CODE = re.compile(r"`((?:raw|reference)/[^`\n]+)`")
PLAIN = re.compile(r"(?<![\w/])(?:raw|reference)/[^\s\]<>\"'`()]+")


def evidence(text, page):
    """Recognize Markdown links, backtick paths and plain raw/reference paths."""
    found = []

    def link(match):
        value = match[1] or match[2]
        parsed = urlsplit(value)
        if not parsed.scheme and not parsed.netloc:
            path = unquote(parsed.path)
            if path.startswith(("raw/", "reference/")):
                found.append(path)
            elif path:
                target = posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
                if target.startswith(("raw/", "reference/")):
                    found.append(target)
        return " "

    remainder = LINK.sub(link, text)

    def code(match):
        found.append(match[1])
        return " "

    remainder = CODE.sub(code, remainder)
    found.extend(match[0].rstrip(".,;:") for match in PLAIN.finditer(remainder))
    return Counter(found)


def validate_edits(changed, previous, available, source_reference=None):
    for page, text in changed.items():
        citations = evidence(text, page)
        old = evidence(previous.get(page, ""), page)
        for target in citations - old:
            content_path(target)
            if target not in available:
                raise ContentError(f"A new evidence citation does not resolve: {target}")
        navigation = not re.sub(r"(?m)^#{1,6} .*?$", "", LINK.sub("", text)).strip(" \t\n-*+0123456789.)")
        if (source_reference and page.startswith("wiki/") and not navigation
                and text.strip() and text != previous.get(page) and not citations):
            raise ContentError(f"Source-derived page {page} needs an original or reference citation.")
