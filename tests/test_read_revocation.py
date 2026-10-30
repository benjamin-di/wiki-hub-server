import asyncio
import threading

import pytest

from wiki_hub.service import Denied


@pytest.mark.parametrize("operation", ["pages", "read_page", "search", "search_empty", "history"])
@pytest.mark.parametrize("waiting", ["lock", "git"])
async def test_revocation_denies_reads_already_waiting(service, monkeypatch, operation, waiting):
    await service.create("owner", "starter", "Starter")
    await service.share("owner", "starter", "reader", "read")
    initial_check = asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    allowed = service.allowed

    def observed(subject, *args, **kwargs):
        result = allowed(subject, *args, **kwargs)
        if subject == "reader":
            initial_check.set()
        return result

    monkeypatch.setattr(service, "allowed", observed)
    if waiting == "lock":
        await service.lock("starter").acquire()
    else:
        name = "history" if operation == "history" else "sync"
        original = getattr(service.git, name)

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(10), "test did not release Git operation"
            return original(*args, **kwargs)

        monkeypatch.setattr(service.git, name, blocked)

    args = {"read_page": ("wiki/index.md",), "search": ("starter",), "search_empty": ("-starter",)}.get(operation, ())
    operation = "search" if operation == "search_empty" else operation
    request = asyncio.create_task(getattr(service, operation)("reader", "starter", *args))
    try:
        await asyncio.wait_for(initial_check.wait(), 5)
        if waiting == "git":
            assert await asyncio.to_thread(entered.wait, 5)
        await service.share("owner", "starter", "reader", "none")
    finally:
        if waiting == "lock":
            service.lock("starter").release()
        release.set()
    with pytest.raises(Denied):
        await asyncio.wait_for(request, 10)


@pytest.mark.parametrize("operation", ["pages", "read_page", "search", "history"])
async def test_cancelled_read_holds_wiki_lock_until_git_finishes(service, monkeypatch, operation):
    await service.create("owner", "starter", "Starter")
    entered, release = threading.Event(), threading.Event()
    name = "history" if operation == "history" else "sync"
    original = getattr(service.git, name)

    def blocked(*args):
        entered.set()
        assert release.wait(10), "test did not release Git operation"
        return original(*args)

    monkeypatch.setattr(service.git, name, blocked)
    args = {"read_page": ("wiki/index.md",), "search": ("starter",)}.get(operation, ())
    request = asyncio.create_task(getattr(service, operation)("owner", "starter", *args))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        request.cancel()
        await asyncio.sleep(0)
        assert service.lock("starter").locked(), "Git thread escaped its wiki lock on cancellation"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
