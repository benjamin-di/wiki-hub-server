"""Bounded, local source extraction. Originals are never rewritten."""

import os
import subprocess
import tempfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from .gitstore import ContentError

MAX_TEXT = 2_000_000
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
TEXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".ics"}


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(text):
    parser = HTMLText()
    parser.feed(text)
    return "".join(parser.parts)


def command(args, timeout=120):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise ContentError("Source extraction is unavailable or exceeded its time limit.") from None
    if result.returncode:
        raise ContentError("Source extraction failed. Check that the file is readable and not encrypted.")
    return result.stdout


def pdf_pages(source):
    text = command(["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"]).decode("utf-8")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def document_text(source):
    pages = pdf_pages(source) if source.suffix.lower() == ".pdf" else []
    derived = not pages or any(not page.strip() for page in pages)
    if derived:
        with tempfile.TemporaryDirectory(prefix="wiki-ocr-") as directory:
            output = Path(directory) / "searchable.pdf"
            command(["ocrmypdf", "--skip-text", "--output-type", "pdf", "--optimize", "0", "--jobs", "1",
                     "--image-dpi", "300", "-l", os.environ.get("WIKI_OCR_LANGUAGES", "eng"),
                     str(source), str(output)], timeout=300)
            pages = pdf_pages(output)
    text = "\n\n".join(f"[Page {n}]\n{page.strip()}" for n, page in enumerate(pages, 1) if page.strip())
    if not text:
        raise ContentError("This document has no readable text after OCR. Check the image quality.")
    return ("Extraction: OCR-derived; verify against the preserved original.\n\n" if derived else "") + text


def email_text(source, depth):
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    headers = "\n".join(f"{key}: {message.get(key, '')}" for key in
                        ("From", "To", "Cc", "Date", "Subject", "Message-ID", "Authentication-Results"))
    parts = ["Email headers (quoted/forwarded identity is unverified):\n" + headers]
    body = message.get_body(preferencelist=("plain", "html"))
    if body:
        try:
            value = body.get_content()
            parts.append(html_text(value) if body.get_content_type() == "text/html" else value)
        except (LookupError, UnicodeError):
            raise ContentError("This email's body encoding could not be read.") from None
    for n, part in enumerate(message.iter_attachments(), 1):
        if n > 20:
            raise ContentError("An email may contain at most 20 attachments.")
        name = part.get_filename() or f"attachment-{n}"
        parts.append(f"\n[Attachment {n}: {name}] (preserved in the original email)")
        # message/rfc822 parts expose a list of Message objects, not a decoded
        # byte payload. Forwarded emails may also have no filename at all.
        attached_email = part.get_content_type() == "message/rfc822"
        suffix = ".eml" if attached_email else Path(name).suffix.lower()
        if suffix not in TEXT | IMAGES | {".pdf", ".eml"}:
            parts.append("Unsupported attachment; download it from the original email.")
            continue
        if attached_email:
            messages = part.get_payload()
            if not isinstance(messages, list) or len(messages) != 1:
                raise ContentError("This attached email could not be read.")
            payload = messages[0].as_bytes()
        else:
            payload = part.get_payload(decode=True)
        if payload is None:
            continue
        with tempfile.TemporaryDirectory(prefix="wiki-mail-") as directory:
            path = Path(directory) / ("attachment" + suffix)
            path.write_bytes(payload)
            parts.append(extract_text(path, depth=depth + 1))
    return "\n\n".join(parts)


def extract_text(source, *, depth=0):
    if depth > 3:
        raise ContentError("Nested email attachments exceed the extraction depth limit.")
    suffix = source.suffix.lower()
    if suffix == ".pdf" or suffix in IMAGES:
        text = document_text(source)
    elif suffix == ".eml":
        text = email_text(source, depth)
    else:
        try:
            text = source.read_text(encoding="utf-8-sig")
        except UnicodeError:
            raise ContentError("This source needs conversion to PDF or UTF-8 text before ingestion.") from None
        if "\x00" in text:
            raise ContentError("This source needs conversion to PDF or UTF-8 text before ingestion.")
        if suffix in {".html", ".htm"}:
            text = html_text(text)
    if len(text) > MAX_TEXT:
        raise ContentError("The extracted source is too large; split it into parts.")
    return text
