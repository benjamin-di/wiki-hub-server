import asyncio
import base64
import json
from dataclasses import replace

import httpx
import pytest
from starlette.testclient import TestClient

from wiki_hub.runner import RunnerSettings, create_app

IDENTIFIER = "a" * 32


@pytest.fixture
def worker(tmp_path):
    settings = RunnerSettings(root=tmp_path, model_url="https://model.example.test/v1/chat/completions",
                              model_name="fixture-model", api_key="synthetic-model-key")
    work = tmp_path / IDENTIFIER
    for folder in ("wiki", "reference", "raw"):
        (work / folder).mkdir(parents=True)
    (work / "AGENTS.md").write_text("Keep source attribution.\n")
    (work / "wiki/index.md").write_text("# Existing index\n")
    (work / f"reference/{IDENTIFIER}.txt").write_text("Source: raw/source.txt\n\nThe workshop opens at 09:30.\n")
    (work / "raw/source.txt").write_text("Original source\n")
    return settings, work


def completion(value, **kwargs):
    return {"choices": [{"finish_reason": kwargs.get("finish_reason", "stop"),
                         "message": {"content": json.dumps(value), **kwargs.get("message", {})}}]}


def test_fixed_model_endpoint_produces_bounded_text_edits_without_tools(worker):
    settings, work = worker
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == settings.model_url
        assert request.headers["Authorization"] == "Bearer synthetic-model-key"
        payload = json.loads(request.content)
        assert "tools" not in payload and "functions" not in payload
        assert payload["store"] is False and payload["max_completion_tokens"] == 8192
        assert payload["response_format"]["json_schema"]["strict"] is True
        supplied = json.loads(payload["messages"][1]["content"])
        assert set(supplied["files"]) == {"AGENTS.md", "wiki/index.md", f"reference/{IDENTIFIER}.txt"}
        return httpx.Response(200, json=completion({"edits": [{
            "path": "wiki/workshop.md", "content": "# Workshop\n\nOpens 09:30. Source: raw/source.txt\n"}]}))

    with TestClient(create_app(settings, httpx.MockTransport(handle))) as client:
        result = client.post("/run", json={"run_id": IDENTIFIER, "vault": IDENTIFIER, "prompt": "File the source."})
        assert result.json() == {"outcome": "success", "exit_code": 0}
        assert client.get("/health").json()["active_runs"] == 0
    assert len(requests) == 1
    assert (work / "wiki/workshop.md").read_text().startswith("# Workshop")
    assert (work / "raw/source.txt").read_text() == "Original source\n"


@pytest.mark.parametrize("path", [
    "AGENTS.md", "../other/wiki/index.md", "raw/source.txt", "wiki/.hidden", "wiki/tool.sh",
])
def test_unsafe_model_batch_applies_no_edits(worker, path):
    settings, work = worker
    response = completion({"edits": [{"path": "wiki/new.md", "content": "Valid first edit"},
                                      {"path": path, "content": "Rejected second edit"}]})
    with TestClient(create_app(settings, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))) as client:
        result = client.post("/run", json={"run_id": IDENTIFIER, "vault": IDENTIFIER, "prompt": "File source."})
        assert result.status_code == 400
    assert not (work / "wiki/new.md").exists()
    assert (work / "raw/source.txt").read_text() == "Original source\n"


def test_job_cannot_select_another_directory_or_read_through_a_symlink(worker):
    settings, work = worker

    def unexpected(_):
        pytest.fail("Invalid filesystem selection reached the model")

    with TestClient(create_app(settings, httpx.MockTransport(unexpected))) as client:
        response = client.post("/run", json={"run_id": IDENTIFIER, "vault": "../other", "prompt": "Read."})
        assert response.status_code == 400
        (work / "wiki/index.md").unlink()
        (work / "wiki/index.md").symlink_to(work.parent / "private.md")
        response = client.post("/run", json={"run_id": IDENTIFIER, "vault": IDENTIFIER, "prompt": "Read."})
        assert response.status_code == 400


def test_provider_failure_is_not_retried_and_cannot_leak_body_or_key(worker, caplog):
    settings, work = worker
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500, text="synthetic-provider-secret synthetic-model-key")

    with TestClient(create_app(settings, httpx.MockTransport(handle))) as client:
        result = client.post("/run", json={"run_id": IDENTIFIER, "vault": IDENTIFIER, "prompt": "File."})
    assert result.status_code == 400 and len(requests) == 1
    assert "synthetic-provider-secret" not in result.text + caplog.text
    assert "synthetic-model-key" not in result.text + caplog.text
    assert (work / "wiki/index.md").read_text() == "# Existing index\n"


def test_incomplete_model_response_cannot_publish_partial_edits(worker):
    settings, work = worker
    response = completion({"edits": [{"path": "wiki/new.md", "content": "Partial"}]}, finish_reason="length")
    with TestClient(create_app(settings, httpx.MockTransport(lambda _: httpx.Response(200, json=response)))) as client:
        response = client.post("/run", json={"run_id": IDENTIFIER, "vault": IDENTIFIER, "prompt": "File."})
        assert response.status_code == 400
    assert not (work / "wiki/new.md").exists()


async def test_application_ingest_uses_standalone_worker_and_preserves_originals(service, monkeypatch):
    settings = RunnerSettings(root=service.settings.workspace_dir,
                              model_url="https://model.example.test/v1/chat/completions", model_name="fixture")

    def model(request):
        supplied = json.loads(json.loads(request.content)["messages"][1]["content"])
        reference = next(path for path in supplied["files"] if path.startswith("reference/"))
        raw = supplied["files"][reference].splitlines()[0].removeprefix("Source: ")
        return httpx.Response(200, json=completion({"edits": [{
            "path": "wiki/workshop.md",
            "content": f"# Workshop\n\nOpens 09:30.\nSource: {raw}\nReference: {reference}\n"}]}))

    worker = create_app(settings, httpx.MockTransport(model))
    native = httpx.AsyncClient

    def clients(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.ASGITransport(app=worker)
        return native(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", clients)
    service.settings = replace(service.settings, ingest_url="http://worker.example.test")
    service.runner = service.run_model
    await service.create("owner", "starter", "Starter")
    original = b"The workshop opens at 09:30.\n"
    job = await service.ingest("owner", "starter", "source.txt", base64.b64encode(original).decode())
    await asyncio.gather(*service.tasks)
    assert (await service.job("owner", job["id"]))["status"] == "completed"
    page = await service.read_page("owner", "starter", "wiki/workshop.md")
    assert "09:30" in page["content"] and f"reference/{job['id']}.txt" in page["content"]
    path = f"raw/{job['id']}-source.txt"
    assert service.git.original(service.state.wiki("starter"), path).read_bytes() == original
