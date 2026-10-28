# Roadmap and remaining work

Updated 27 October 2026. This is the reusable product roadmap. Deployment
inventories, account bindings, legacy imports and acceptance receipts belong
in private operations records. Source implementation, release publication and
acceptance on an installation are separate milestones.

## Product standard and architecture

Deliver reliable, source-backed knowledge that people and scoped agents can
read, update and synchronize. Judge quality by evidence preserved, useful facts
retrieved, attributable corrections, honest failure states and independent
recovery. A successful model call or Git commit alone proves none of these.

Keep one application worker, SQLite, Forgejo 17, Git LFS and an external identity
provider. Git stores accepted content and history; SQLite stores the control
plane and job journal. Search is derived and rebuildable. Extend these existing
boundaries before adding another service.

The application owns privileged Git and publication. Model workers stay within
one wiki's job, cannot edit root instructions and receive no application or Git
credential. External workers must enforce the same isolation independently.

| Component | Responsibility |
|---|---|
| Identity provider | Human accounts, passkeys, service admission, OAuth clients and token issuance |
| Wiki Hub | Wiki ownership, explicit grants, agent bindings, copy settings, ingestion and retrieval |
| Forgejo | Accepted Git history, LFS originals and projected Git access |
| Device CLI | Managed copies and explicit Git synchronization |
| Consuming applications | Executable releases, calculations, generated artifacts and mutable state |

Use the configured issuer and immutable subject for identity. Provider groups
control admission; they grant no wiki. Independent agents have explicit grants
capped by their owner's current access. Gateway admission and per-wiki access
are separate. Shared identity has independent ownership, backup and recovery;
a wiki restore must not overwrite its production state.

Sources establish what they say, not whether their claims are true. Preserve
originals and faithful extractions, keep conflicting accounts attributable and
let dated corrections change the current account without erasing its evidence.
Ordinary accepted ingests save automatically. Deterministic validation and
measured model quality have different jobs; no general fact-verification service
or mandatory per-ingest human approval is planned.

## Implemented baseline and known limits

These capabilities exist in source, separately from installation acceptance.

| Capability | Implemented behavior |
|---|---|
| Knowledge | Private wikis, preserved originals and extracted references, lexical search, history and rollback |
| Human access | Explicit read/contribute/write/admin grants; the configured owner creates wikis and registers agents |
| Local copies | User/wiki copy settings, scoped Git credentials and current permission checks before synchronization |
| Device sync | Single-copy and batch sync, opt-in discovery, exclusions, per-copy locks and structured failures |
| Agent access | Separately revocable keys or explicit Pocket ID workload bindings; read/contribute ceiling and no managed Git copies |
| Offboarding | Local suspension blocks human sessions and owned agents; backend revocation remains pending until confirmed |
| Archival | Reads and evidence retained, writes stopped, unfinished ingests interrupted and Git credentials revoked |

The source now includes the small correctness foundation; installation acceptance
and populated quality measurements remain separate:

- Publication validates preserved bytes and new evidence citations in the final
  tree. Factual support still requires the quality evaluation below.
- Submission IDs bind retries to one job. Candidate commits are journaled before
  push; uncertain outcomes reconcile against accepted history when queried.
- Authorized job listing and bounded intake use the existing application/state.
- Search fetches Git and uses a rebuildable SQLite FTS5 index at that accepted
  revision; failure falls back to a content scan. Page/reference and original reads pin a full accepted revision
  while enforcing current access.
- The bundled worker selects complete relevant pages within its default
  256,000-byte budget and rejects replacement of omitted existing files. Complete
  sources must fit. External workers need their own size/attempt qualification.

Implementation anchors: [publication and retrieval](../src/wiki_hub/service.py),
[Git operations](../src/wiki_hub/gitstore.py), [job journal](../src/wiki_hub/state.py)
and [bundled worker](../src/wiki_hub/runner.py). The milestones below retain the
contracts and required evidence; implemented source alone does not close them.

## Delivery sequence and dependencies

| Milestone | Completion evidence | Dependency |
|---|---|---|
| A. Qualify the server release | Exact source/image, copied-state upgrade, authenticated reads and usable recovery point | First deployment step |
| B. Make ingestion outcomes recoverable | Submission replay, publication reconciliation, discoverable jobs and bounded intake | Before unattended production ingestion |
| C. Prove a populated knowledge workflow | Evidence replay, quality cases, measured size limits and independent restore | Before activating that workflow |
| D. Accept each consuming client | Actual permissions, transport and synchronization through its installed entry point | Before switching that client |
| E. Expand and stabilize | Each collection passes B/C and its consumers pass D; routine operation observed | Before broader adoption or retirement |

B and C can be developed and rehearsed together. D proceeds independently for
each client. An unavailable Windows device or second human does not block an
agent-only pilot; their own acceptance remains required before their adoption.
A qualified baseline upgrade need not wait for every future feature, but does
not establish acceptance of the outstanding workflows.

### A. Release and recovery

- Keep tested source, immutable image and installed CLI identities explicit.
  Builds, published tags and running upgrades are different facts. Fix forward
  through a new release; never move a published tag to include later work.
- Rehearse schema changes on copied state. Retain the previous image,
  configuration and matching snapshot; image rollback is not data rollback.
- Check an existing sign-in, known page, original download, actual grants and
  controlled ingest. Qualify the worker actually configured on the installation.
- Restore populated Git/LFS content, catalog and grants independently. Check
  hashes and authenticated retrieval, including originals. Keep shared production
  identity outside the rehearsal. See [operations](operations.md).
  Before exposing recovered access, reconcile suspensions and revocations made
  since the snapshot; restoring old grants must not silently revive old access.
- Public exports and workflows use public dependencies and GITHUB_TOKEN.
  Supported-platform CI and installed-client acceptance remain distinct gates.

### B. Recoverable ingestion with a small durable journal

Use existing SQLite jobs, Git job trailers and retained worktrees. Add the
minimum durable fields and transitions to distinguish model work, publication
intent and an observed accepted revision. No external queue is needed.

- Let CLI, API and MCP callers reuse a submission identifier scoped to actor
  and wiki. Bind it to source bytes, filename and filing instructions. Concurrent
  or restarted replays return the same job without another model call. Refuse
  changed input under the same ID. Check current access before returning a replay;
  byte-identical deliberate submissions may use new IDs.
  Clients retain the ID before sending so a lost reply can be queried or replayed.
  Replay-required HTTP submissions use a distinct endpoint, so older servers
  cannot silently ignore the ID and start unkeyed model work.
- Record the final candidate revision before pushing. After a lost reply or
  restart, observe whether that exact commit is reachable from accepted main
  history, including when later commits exist. An accepted commit completes the
  job; an unobservable result stays explicitly uncertain. A local object alone
  is insufficient evidence of acceptance. Absence from one fresh fetch cannot
  establish rejection while the remote may still be processing a lost reply;
  distinguish an explicit Git rejection from that uncertain outcome.
- Recovery records outcomes without rerunning a model or automatically
  republishing retained work. An explicit later publication must recheck current
  grants, archival and conflicts. Apply the same outcome discipline to rollback,
  which also publishes a commit.
- Expose bounded, authorized job listing through the existing interfaces so
  lost response IDs and failed/interrupted/uncertain jobs can be found. Keep
  source paths, credentials and provider diagnostics private.
- Bound queued work before accepting another source; return a clear busy result
  without a partly accepted job. Keep one model slot and per-wiki serialization.
  Defer fairness scheduling, cancellation APIs and per-user billing.

Acceptance uses disposable repositories and fake workers: concurrent duplicate
submissions, lost submission replies, accepted push with lost acknowledgement,
restart between push and journal update, later remote commits, definite rejection,
unavailable backend, queue saturation and permission loss. Assert model-call
counts, remote history and durable job outcomes. No force merge or push.

### C. Useful knowledge and reproducible evidence

Start with [synthetic acceptance cases](../examples/acceptance.md), then evaluate
one representative populated collection through its actual consumer. Keep real
evidence and receipts private. The service retrieves knowledge; answer quality
also depends on the consuming agent and its prompt.

**Evidence contract.** Add optional revision-pinned page/reference and original
reads to existing API, MCP and CLI surfaces where applicable. Resolve full commit
IDs only within that wiki's accepted main history. Search results, source locators
and downloaded bytes must agree at the selected revision while current grants
still apply. Test an intervening update, rollback and revocation. Imported legacy
history retained only in a private archive is outside this API contract.

**Publication checks.** Define a small supported convention for local evidence
citations. Validate new or changed evidence links against the final tree,
including a concurrent merge. Substantive source-derived edits need attributable
evidence; navigation-only edits need not repeat every citation. Report existing
broken links separately so one legacy link does not block unrelated work. Link
existence cannot prove factual support; page numbers need real extraction locators.

**Quality gate.** Expand the fixed corpus to at least 30 labelled questions:
ordinary facts, exact values/units/dates, paraphrases, conflicting accounts, dated
corrections, absent information and source instruction attacks. Inspect generated
pages as well as consumer answers. For the evaluated sample:

- Originals and references retain their expected bytes; new evidence citations
  resolve. No unexplained unrelated fact is removed.
- Relevant evidence appears in the first five results for at least 90% of
  answerable questions and every predesignated critical question.
- Critical value/date/correction answers are supported and correctly attributed.
  Missing information is not invented; conflicts are not silently collapsed.
  Record individual failures as well as aggregate counts.

These are initial pilot targets, not a universal accuracy claim. Freeze expected
evidence before qualification. CI uses synthetic fixtures and fake responses;
bounded model/consumer evaluation is a separate explicit run with recorded
worker/model settings and review. No paid model in `make check`, automatic judge
service, best-of-many retries or silent regeneration of imports.

**Supported size and performance.** Record file counts, searchable bytes, largest
source/page/original, search latency, ingest duration, peak memory and worker
context/output use for the pilot and next intended batch. Set accepted latency
and resource budgets before qualification; retain measurements. Test writes and
revocation while retrieval is busy. Text/PDF extraction limits and original
download capacity are separate facts. Unsupported formats and scanned PDFs need
explicit conversion; a general OCR/conversion platform is deferred.

If the bundled worker's whole-wiki budget is exceeded, implement bounded relevant
page selection before declaring that collection writable. Supply complete target
pages before full-file replacement; never silently truncate a source or replace
a page from a partial view. Oversized sources stay explicit failures requiring
reviewed splitting/conversion. External workers must prove equivalent preservation
and resource bounds without inheriting an assumed 256 KB limit.

If scanning misses the agreed quality/latency budget, use a revision-keyed,
rebuildable SQLite FTS5 index first; it supports [ranking and snippets](https://sqlite.org/fts5.html).
Account for Git-client pushes and rollback, not just ingest commits. Return an
explicit indexed revision or correct fallback while rebuilding; never label stale
passages with a newer revision. Index failure cannot undo accepted Git data.
Embeddings, rerankers and additional storage wait for a measured remaining gap.

### D. People, agents and everyday copies

Human enrollment stays in the identity provider; wiki sharing stays in the Hub.
Verify isolation, explicit sharing, selected roles and denial after unshare or
local suspension. Provider removal may leave issued tokens valid until expiry;
immediate offboarding also needs local suspension and Git credential revocation.
Pending backend changes never count as completed revocation.

For agents, test disjoint grants, owner ceiling, read versus contribute, denied
Git/admin operations and revocation in an existing connection. Unfinished ingests
cannot publish after revocation succeeds; admitted publication may finish before
it returns. Qualify actual SDK token exchange, audience, scope, signed identity
and lifetime for [workloads](clients.md#pocket-id-workloads). The stateless OAuth
challenge requires POST; HEAD/GET are not authentication probes.

Restrict alternate filesystem mounts and human credentials before claiming a
workflow is confined to MCP grants. Move a real workflow before retiring its old
access; normal deployment must not restore revoked bindings or grants.

Use `wiki sync --all --root DIRECTORY --json`, adding `--discover` only when a
device opts in. Existing schedulers are sufficient. Verify two-device edits,
discovery, offline/excluded work, conflicts, readers, archive/reopen, Git/LFS
revocation, locks and per-copy failure reporting through installed clients.
One clone is insufficient evidence of automatic-sync parity.

Keep one executor per physical checkout. Windows/WSL sharing uses the native
Windows CLI/keyring; separate WSL clones have their own sign-in. Windows copies
stay outside cloud-sync folders. Preserve folders/aliases and make generic Git
sync skip managed copies. Test native Windows and each supported hosted client
before adopting it. See [device sync](clients.md#device-sync).

### E. Populated adoption and application boundaries

Activate one small collection with its actual scoped consumer once B/C and that
consumer's D checks pass. Observe normal use, corrections, job failures and backup
recovery before expanding. Inspect backlog age, pending permission projections
and disk growth with existing operator tools and schedulers. `/health` proves
process availability; authenticated retrieval proves more. Neither should spend
model calls merely to report health.

Keep explanations, decisions, curated records, originals and selected accepted
reports in the wiki. Generated evidence can be useful; an extension or `output/`
directory alone is not grounds to discard it. Executable code, tests, calculation
data, current-report pointers, cursors, receipts and retry state belong to the
consuming application's existing release/storage boundary. Record input revisions
and original-byte hashes, preserve manual recovery paths and protect linked artifacts.

Legacy transfer is a private one-off operation, not a public importer or ongoing
compatibility promise. Source capture, consumer cutover, independent recovery and
conditional rollback belong together. Advance each collection when its own gates
pass; unrelated application extraction or client work need not hold it back.

## Deliberate deferrals

Keep group synchronization, SCIM, background watchers, extra agent registries,
new sync daemons, general approval engines, distributed queues and new retrieval
platforms deferred. Add capabilities when a named workflow misses a measured
target, using the smallest change within the existing system. Best-in-class for
this scope means demonstrated reliability and useful knowledge at a stated
operating size, with understandable recovery and low maintenance cost.
