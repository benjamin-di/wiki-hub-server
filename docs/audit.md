# Source audit — 30 October 2026

Reviewed the access/control plane, Git publication and recovery, retrieval,
extraction, managed-copy CLI and Windows drive flows against their documented
contracts. This records source findings and offline regressions; it does not
certify a deployed image, model quality or a real Windows computer.

## Correctness fixes

| Trigger | Previous failure | Corrected behavior |
|---|---|---|
| Cancel a read, index rebuild or source preview | The coroutine released its wiki lock, extraction slot or temporary source while its OS thread kept running | Drain the worker before releasing those resources, including repeated cancellation. Publication and credential projections use the same drain discipline |
| Disconnect while the file server cannot confirm revocation | CLI forgot the device, preventing retry; PowerShell reported completion and skipped that revoked device on the next run | Retain the device until confirmation, retry pending devices for that computer, and report pending status. CLI disconnect/revoke returns nonzero while pending; stale or absent observations cannot prove revocation |
| Ingest an email with a standard `message/rfc822` attachment | The MIME API returned nested messages rather than bytes, and extraction silently skipped their content | Extract nested headers, bodies and supported attachments, with the existing depth limit, while preserving original email bytes |
| Download an original whose streaming HTTP reply is an error | Accessing the unread response raised an internal stream exception and left cleanup incomplete | Inspect a bounded error body, report the HTTP failure and close the response without saving a file |
| Submit the full allowed source bytes with Unicode filing instructions or multiple parts | The HTTP envelope limit rejected valid input before per-source validation | Reserve bounded metadata and base64-padding space while keeping the shared decoded-byte limit unchanged |
| Direct Git push replaces a drive-edited page or its `wiki/` parent with a symlink | Publication wrote into the new worktree before checking file modes, so a write could follow the link outside the job even though publication subsequently failed | Validate accepted content modes before applying any drive edit; reject symlinks and nested repositories, retain the unpublished view and record a failed job |
| Search common terms, fall back after index failure, or browse a wiki with unusual Git filenames | Rounding erased small relevance differences; fallback ignored filenames; unsupported names could break retrieval | Preserve BM25 distinctions, include supported filenames in fallback, and skip unsupported paths without changing accepted content |
| Discover wikis while a backend stalls or after a direct Git push | Overview refresh could block the listing or describe an older local checkout | Fetch and read the remote accepted index under short per-wiki and shared budgets; retain wiki metadata when an overview is unavailable and recheck current access |

The reproductions failed on the reviewed baseline before the fixes. All endpoints
and sources in the regressions are synthetic; tests use disposable Git/LFS state,
fake identity/file-server/model endpoints and mocked Windows operations. The
PowerShell disconnect regression executes the served functions in PowerShell.

Regression anchors:

- [Cancellation and resource ownership](../tests/test_cancellation.py).
- [Drive CLI and confirmation](../tests/test_drive_client.py),
  [PowerShell disconnect](../tests/test_drive_windows.py) and
  [download/CLI exit behavior](../tests/test_cli.py).
- [Email and complete HTTP submissions](../tests/test_extraction.py).
- [Drive publication before filesystem writes](../tests/test_drive.py), including
  a regular file becoming a symlink with the same Git blob ID.
- [Retrieval and bounded discovery](../tests/test_retrieval.py),
  [index ranking](../tests/test_search_index.py) and
  [permission/filename edge cases](../tests/test_hardening.py).

The optional trusted-gateway transport is also implemented. Its backend key
authorizes only its separate MCP endpoint, its human subject comes from the
gateway, and every operation still checks current per-wiki grants. Direct OAuth
and separately scoped agents retain their existing credential boundaries.
See [gateway regressions](../tests/test_gateway.py) and
[client setup](clients.md#hosted-claude-and-chatgpt).

The archive/publication test now signals its checkpoint to the event loop
instead of occupying an executor worker while waiting. A single-worker executor
reproduced the old fixture's starvation and is retained in the regression;
publication must still finish before archive returns. This changes test
synchronization, not production archive ordering.

## Boundaries and remaining work

The implementation retains current-grant checks, explicit agent owner ceilings,
pending backend projections, immutable originals/references and uncertain
publication outcomes. The bundled worker has no privileged tools and cannot
replace a page omitted from its context. These remain regression contracts,
not a claim that the audit found every possible bug.

No schema migration is required by these fixes. Cancellation may wait for an
already running bounded Git/extraction operation to finish; abandoning its lock
would restore the race. Archive remains a retention operation, and writable
inbox provisioning alone does not implement capture, consent or storage quotas.

The remaining product integration is the optional writable inbox's consent-aware
consumer and storage limits, if that workflow is wanted. Core agent discovery,
search, revision-pinned reads, recoverable ingestion and editable drive pages
already exist. Release/publication, installed-client checks, representative
knowledge/size measurements and a populated restore are acceptance work rather
than missing retrieval features. No new retrieval service is required by this
audit.

The [roadmap](roadmap.md#outstanding-work-after-the-source-audit) separates
remaining implementation/integration from model-quality measurements, release
delivery and installed-client acceptance. Keep fleet inventories and live
receipts in private operations records.
