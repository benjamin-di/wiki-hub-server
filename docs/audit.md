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

## Follow-up storage and confirmation fixes

The adjacent audit added these synthetic regressions. Each failure was reproduced
on the reviewed baseline before its fix; the Unicode boundary case also verifies
that a supported name still preserves the exact original bytes.

| Trigger | Previous failure | Corrected behavior |
|---|---|---|
| Submit a long Unicode filename, including an attachment | Character-count validation allowed a name that exceeded the filesystem limit, sometimes only after the accepted job added its original-source prefix | Validate the 222-byte UTF-8 filename limit before staging any part or accepting a job; keep the existing 120-character limit |
| Rebuild a writer's view after metadata loss while a directory cannot be enumerated | The recovery walker silently skipped its unpublished files, then the rebuild discarded the old view | Refuse the incomplete recovery scan and preserve the view until every directory can be read; retain its files before rebuilding |
| Retry revocation against an old file-server report at the expected generation | The operation could report confirmation even though device status correctly reported the same observation as stale | Require a report no older than 60 seconds before acknowledging the requested account change; revocation stays pending until then |

Regression anchor: [storage and confirmation failures](../tests/test_audit_regressions.py).
These changes require no schema migration and do not truncate or rename sources.
An unreadable directory prevents a complete recovery copy; operators must restore
its readability before rebuilding or withdrawing an unknown view.

## Browser and sharing audit

The initial browser implementation was also reviewed for OIDC/session boundaries,
current grants, directory admission, durable grant intent, explicit retries,
optimistic concurrency and failure recovery. It calls the same service as the
bearer APIs; cookies authorize only the browser routes.

| Trigger | Previous failure | Corrected behavior |
|---|---|---|
| One browser refresh waits on the identity provider | A global refresh lock stalled all signed-in browsers | Serialize rotation per session; independent browsers continue, and completed locks are released |
| The bounded unfinished-sign-in table is full | Transaction creation escaped the login error handler and returned an unhandled error | Return a safe, uncached, retryable 503 sign-in page |
| A selected directory account is deleted before submission | A directory 404 appeared as a temporary service outage | Reject the selection with a 400 before recording permission intent; actual provider failures remain unavailable |
| Focus/polling overlaps a successful role change | An older snapshot could replace the confirmed membership shown on screen | Ignore out-of-order refreshes and snapshots older than the currently rendered access version |
| A manager edits two member rows and saves one | Refreshing confirmed membership silently discarded the other unsaved role | Preserve unchanged members' drafts and their Save controls without submitting them |
| Refresh returns a structured temporary OAuth error | `server_error` or `temporarily_unavailable` was treated as a revoked grant and deleted the browser session | Return a retryable 503 and retain the encrypted session; the next request can refresh after recovery. A revoked `invalid_grant` still ends the session |

Regression anchors: [signed synthetic identity and HTTP tests](../tests/test_web.py)
and [real Chromium acceptance](../tests/test_web_browser.py). Existing regressions
cover grant projection failures/restart, receipt replay, superseded retries,
stale versions, reader isolation, local suspension and connected revocation.
All identity, directory and backend endpoints are synthetic. The five new
regressions reproduce their failures against the original browser implementation.

Validation: `make check` passed with 475 tests and five skips (four opt-in browser
cases and the platform-dependent check); `make check-browser` passed all four
Chromium cases. JavaScript syntax and whitespace checks passed. All 12 browser
modules/templates/assets in the built wheel match source. These checks establish
source behavior, not real identity or installed-browser acceptance.

Unlike the earlier fixes, the browser release adds SQLite tables for encrypted
login/session state, display profiles, access versions and operation receipts.
The copied-state rehearsal preserves existing ownership/grants and leaves its
source database untouched. Live identity acceptance
and human usability remain explicit [web follow-up work](web-ui.md#implementation-status-and-remaining-work).

### Browser follow-up audit

Two additional regressions cover the browser/client boundary and restored pages:

| Trigger | Previous failure | Corrected behavior |
|---|---|---|
| Register the configured browser client as an agent workload | The retained workload binding made subsequent human browser tokens fail authentication, including after agent revocation | Reject both configured human-login client IDs before creating a workload binding |
| Restore the Share page from the browser's page cache after access was removed | The cached member list and open sharing dialog remained visible without a focus or visibility event | Recheck current management access on a persisted `pageshow` event and clear protected content on denial |

The signed synthetic HTTP regression checks that rejected registration preserves
the current session and a fresh login. The Chromium regression revokes a separate
browser's grant through the bearer API, then restores its Share page with an open
dialog. These fixes add no schema changes and do not alter existing grants or
identity-provider registrations. Real sign-in and a second person's own session
remain installation acceptance work.

### Browser administration recovery

Lost credential-creation replies lead to review and revocation before a
replacement is created; partial member-label refresh and pending settings have
specific status messages. Sharing confirmation waits for the refreshed access
snapshot, so a subsequent change uses its current version. An unavailable
snapshot leaves an explicit reload instruction rather than a claim that current
membership is displayed, including when a lost response is recovered through its
operation receipt.

The [browser regressions](../tests/test_web_browser.py) exercise lost replies,
delayed and unavailable snapshots, device confirmation, account suspension and
archive recovery with fake identity, Git and file-server endpoints. This adds
no schema or general administrative operation framework. Installation and human
usability acceptance remain separate.

## Boundaries and remaining work

The implementation retains current-grant checks, explicit agent owner ceilings,
pending backend projections, immutable originals/references and uncertain
publication outcomes. The bundled worker has no privileged tools and cannot
replace a page omitted from its context. These remain regression contracts,
not a claim that the audit found every possible bug.

The earlier correctness fixes require no schema migration; the browser additions
are described above. Cancellation may wait for an
already running bounded Git/extraction operation to finish; abandoning its lock
would restore the race. Archive remains a retention operation, and writable
inbox provisioning alone does not implement capture, consent or storage quotas.

Browser administration controls are implemented; installed identity/directory and
human acceptance remain separate. Remaining product work includes integrating
the optional writable inbox's consent-aware consumer and storage limits if that
workflow is wanted. Core agent discovery,
search, revision-pinned reads, recoverable ingestion and editable drive pages
already exist. Release/publication, installed-client checks, representative
knowledge/size measurements and a populated restore are acceptance work rather
than missing retrieval features. No new retrieval service is required by this
audit.

The [roadmap](roadmap.md#outstanding-work-after-the-source-audit) separates
remaining implementation/integration from model-quality measurements, release
delivery and installed-client acceptance. Keep fleet inventories and live
receipts in private operations records.

## Additional filesystem regressions

Two further reproductions failed on the reviewed baseline and now pass with
synthetic sources and disposable repositories:

| Trigger | Previous failure | Corrected behavior |
|---|---|---|
| Drive folder enumeration fails | The scan inferred deletions for accepted pages it could not enumerate | Keep pages beneath that folder, report it in the existing attention diagnostics and retry scanning; independent readable edits and actual deletions still proceed |
| A valid wiki folder has a name ending in `.md` | The standalone worker tried to read the folder as a page and failed before calling the model | Skip the folder itself while including its Markdown child pages, after the existing path safety check |

Regression anchors: unreadable root/nested folders and recovery in
[test_drive.py](../tests/test_drive.py), and complete nested-page context in
[test_runner.py](../tests/test_runner.py). These fixes require no schema change.

The drive troubleshooting settings now match activation in source. Operations
guidance also makes cold-copy downtime explicit: the journal cannot accept work
while the process is stopped, and consumers retain unchanged submission IDs and
originals for inspection or explicit replay after recovery.

## Browser administration completion

Settings/archive, agents, drive devices and owner-only account controls now call
the existing service methods. They preserve pending backend intent and show
one-time credentials without browser storage. Member-label refresh preserves
grants through directory outages. Signed provider logout includes replay and
concurrent-login fencing; protected pages recheck current access on focus.
Sharing waits for its initial authorized snapshot before enabling new grants.

The expanded offline identity/service suite and six Chromium cases cover these
controls, pending copy retries, secret dismissal, agent revocation and manager
revocation while settings remain open. Real provider, file-server, passkey and
human usability acceptance remain installation work, recorded separately.
