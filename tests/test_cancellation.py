"""Cancellation must not release resources still used by synchronous workers."""

import asyncio
import threading

import pytest

from wiki_hub.search import SearchIndex


@pytest.mark.parametrize("operation", ["pages", "read_page", "search", "history", "index"])
async def test_cancelled_reader_holds_wiki_lock_until_worker_finishes(service, monkeypatch, operation):
    await service.create("owner", "starter", "Starter")
    entered, release = threading.Event(), threading.Event()
    if operation == "index":
        target, method = SearchIndex, "search"
    else:
        target, method = service.git, "history" if operation == "history" else "sync"
    original = getattr(target, method)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, blocked)
    name = "search" if operation == "index" else operation
    args = {"read_page": ("wiki/index.md",), "search": ("starter",)}.get(name, ())
    request = asyncio.create_task(getattr(service, name)("owner", "starter", *args))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        request.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # fetch/merge and index rebuild still use the canonical repository.
        assert service.lock("starter").locked()
        assert not request.done()
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    assert request.cancelled()
    assert not service.lock("starter").locked()


async def test_cancelled_preview_keeps_source_and_slot_until_extraction_finishes(service, monkeypatch):
    await service.create("owner", "starter", "Starter")
    entered, release = threading.Event(), threading.Event()
    seen = []

    def blocked(path):
        entered.set()
        assert release.wait(10)
        seen.append(path.read_text())
        return seen[-1]

    monkeypatch.setattr("wiki_hub.service.extract_text", blocked)
    request = asyncio.create_task(service.extract_preview("owner", "starter", "note.txt", "bm90ZQ=="))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        request.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert service.model_slot.locked()
        assert not request.done()
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    assert seen == ["note"]
    assert request.cancelled() and not service.model_slot.locked()
