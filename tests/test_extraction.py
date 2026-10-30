"""Synthetic source extraction and atomic multi-part submission acceptance."""
import asyncio
import base64
from email.message import EmailMessage
from unittest.mock import Mock

import httpx
import pytest
from test_http_mcp import FixtureVerifier

from wiki_hub import extraction
from wiki_hub.gitstore import ContentError
from wiki_hub.server import create_app
from wiki_hub.service import Denied


def encoded(text):
    return base64.b64encode(text.encode()).decode()


def test_email_preserves_headers_body_and_attachments(tmp_path):
    message = EmailMessage()
    message["From"] = "sender@example.com"
    message["Subject"] = "Workshop receipt"
    message.set_content("Workshop total: 123.40")
    message.add_attachment(b"Workshop starts at 09:30", maintype="text", subtype="plain", filename="schedule.txt")
    path = tmp_path / "forward.eml"
    original = message.as_bytes()
    path.write_bytes(original)
    text = extraction.extract_text(path)
    assert "sender@example.com" in text and "123.40" in text and "09:30" in text
    assert "unverified" in text and "Attachment 1" in text
    assert path.read_bytes() == original


@pytest.mark.parametrize("filename", ["confirmation.eml", None])
def test_attached_email_is_extracted_with_its_own_headers_and_body(tmp_path, filename):
    forwarded = EmailMessage()
    forwarded["From"] = "vendor@example.com"
    forwarded["Subject"] = "Workshop confirmation"
    forwarded.set_content("The workshop opens at 09:30.")
    forwarded.add_attachment(b"Total: 123.40", maintype="text", subtype="plain", filename="receipt.txt")
    message = EmailMessage()
    message.set_content("Please file the attached confirmation.")
    message.add_attachment(forwarded, **({"filename": filename} if filename else {}))
    source = tmp_path / "forward.eml"
    original = message.as_bytes()
    source.write_bytes(original)
    text = extraction.extract_text(source)
    assert "vendor@example.com" in text and "09:30" in text and "123.40" in text
    assert source.read_bytes() == original


def test_html_does_not_fetch_remote_content(tmp_path):
    path = tmp_path / "source.html"
    path.write_text('<p>Keep this</p><script>ignore this</script><img src="https://example.com/track">')
    assert "Keep this" in extraction.extract_text(path)
    assert "ignore" not in extraction.extract_text(path)


def test_mixed_pdf_ocr_retains_page_locators_and_original(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"synthetic original")
    pages = Mock(side_effect=[["Text layer", ""], ["Text layer", "Recognized second page"]])
    monkeypatch.setattr(extraction, "pdf_pages", pages)
    calls = []
    monkeypatch.setattr(extraction, "command", lambda args, **kw: calls.append(args))
    text = extraction.extract_text(path)
    assert "OCR-derived" in text and "[Page 2]" in text and "Recognized" in text
    assert "--skip-text" in calls[0]
    assert path.read_bytes() == b"synthetic original"


async def test_preview_does_not_store_and_rechecks_access(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "read")
    with pytest.raises(Denied):
        await service.extract_preview("reader", "starter", "note.txt", encoded("private text"))
    result = await service.extract_preview("owner", "starter", "note.txt", encoded("private text"))
    assert result["text"] == "private text"
    assert not service.state.pending_jobs()
    def revoke(source):
        service.state.set_grant("starter", "reader", "none")
        return "private text"
    await service.share("owner", "starter", "reader", "contribute")
    monkeypatch.setattr("wiki_hub.service.extract_text", revoke)
    with pytest.raises(Denied):
        await service.extract_preview("reader", "starter", "note.txt", encoded("private text"))


async def test_bundle_is_one_model_job_with_all_originals(service):
    await service.create("owner", "starter", "Starter")
    extra = [{"filename": "attachment.txt", "content_base64": encoded("Total: 123.40")}]
    job = await service.ingest("owner", "starter", "message.md", encoded("Workshop receipt"),
                               submission_id="bundle-1", attachments=extra)
    await asyncio.gather(*service.tasks)
    result = await service.job("owner", job["id"])
    assert result["status"] == "completed", result
    wiki = service.state.wiki("starter")
    first = await asyncio.to_thread(service.git.original, wiki, f"raw/{job['id']}-message.md")
    second = await asyncio.to_thread(service.git.original, wiki, f"raw/{job['id']}-attachment.txt")
    assert first.read_text() == "Workshop receipt"
    assert second.read_text() == "Total: 123.40"
    replay = await service.ingest("owner", "starter", "message.md", encoded("Workshop receipt"),
                                  submission_id="bundle-1", attachments=extra)
    assert replay["id"] == job["id"]
    with pytest.raises(ContentError, match="different input"):
        await service.ingest("owner", "starter", "message.md", encoded("Workshop receipt"),
                             submission_id="bundle-1", attachments=[])


async def test_http_bundle_allows_full_source_limit_and_unicode_instructions(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    monkeypatch.setattr("wiki_hub.service.extract_text", lambda _path: "Synthetic extracted text.")
    data = base64.b64encode(b"x" * (service.settings.upload_limit - 19)).decode()
    attachments = [{"filename": f"part-{n}.txt", "content_base64": encoded("x")} for n in range(19)]
    app = create_app(service.settings, service, FixtureVerifier())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://wiki.example.test") as client:
        response = await client.post("/api/wikis/starter/submissions", headers={"Authorization": "Bearer owner-token"},
                                     json={"filename": "source.txt", "content_base64": data,
                                           "instructions": "文" * 4000, "submission_id": "unicode-bundle",
                                           "attachments": attachments})
    assert response.status_code == 202, response.json()
    await asyncio.gather(*service.tasks)
    assert (await service.job("owner", response.json()["id"]))["status"] == "completed"


async def test_cross_owner_workload_ceiling_and_revocation(service):
    await service.create("owner", "starter", "Starter")
    agent = await service.add_workload_agent("owner", "Intake", "intake-reader", owner="reader")
    assert agent["owner"] == "reader"
    assert agent["id"] in [a["id"] for a in await service.agents("reader")]
    with pytest.raises(Denied):
        await service.add_workload_agent("reader", "Intake", "another", owner="owner")
    with pytest.raises(Denied):
        await service.share("owner", "starter", agent["id"], "contribute")
    await service.share("owner", "starter", "reader", "contribute")
    await service.share("owner", "starter", agent["id"], "contribute")
    await service.revoke_agent("reader", agent["id"])
    with pytest.raises(Denied):
        await service.extract_preview(agent["id"], "starter", "note.txt", encoded("secret"))


async def test_contributor_undo_is_own_latest_ingest_only(service):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "contribute")
    job = await service.ingest("reader", "starter", "one.txt", encoded("First source"))
    await asyncio.gather(*service.tasks)
    with pytest.raises(Denied):
        await service.undo_ingest("owner", job["id"])
    result = await service.undo_ingest("reader", job["id"])
    assert result["undone"] == (await service.job("reader", job["id"]))["revision"]
    with pytest.raises(ContentError, match="wiki changed"):
        await service.undo_ingest("reader", job["id"])
    latest = await service.ingest("reader", "starter", "two.txt", encoded("Second source"))
    await asyncio.gather(*service.tasks)
    await service.share("owner", "starter", "reader", "read")
    with pytest.raises(Denied):
        await service.undo_ingest("reader", latest["id"])


def test_attached_email_depth_limit_is_enforced(tmp_path):
    message = EmailMessage()
    message.set_content("Nested synthetic source.")
    for _ in range(4):
        outer = EmailMessage()
        outer.set_content("Attached message.")
        outer.add_attachment(message, filename="forwarded.eml")
        message = outer
    path = tmp_path / "nested.eml"
    path.write_bytes(message.as_bytes())
    with pytest.raises(ContentError, match="depth limit"):
        extraction.extract_text(path)
