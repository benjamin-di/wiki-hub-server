# Roadmap and remaining work

Audited 30 October 2026. This is the reusable product roadmap. Deployment
inventories, account bindings, legacy imports and acceptance receipts belong
in private operations records. Source implementation, release publication and
acceptance on an installation are separate milestones.

## Immediate scope for agent knowledge

The core MCP retrieval features are implemented: discovery instructions,
cross-wiki lexical search, stemming/phrases/exclusions, title weighting, heading
snippets, wiki overviews, file maps, numbered range reads and path suggestions.
The latest retrieval fixes preserve small relevance scores, ignore unsupported
Git filenames, search filenames during fallback, refresh overviews after direct
pushes and recheck access before returning search outcomes.

No additional core retrieval feature is required for the immediate agent workflow.
Built-in MCP consultation instructions and a reusable
[host-instruction example](clients.md#help-agents-consult-the-wiki) already exist.
The optional trusted-gateway transport is implemented with a separate backend
credential and current per-wiki authorization; it does not replace direct OAuth
or scoped agent access.

The immediate product promise is to submit information once, preserve it
faithfully, retrieve supported answers, enforce current access and recover
failures without losing work. Prioritize completion of that workflow over new
screens or transports.

| Order | Work | Observable completion |
|---|---|---|
| 1 | Finish the correctness release | Confirmed retention, filename and revocation defects are fixed; the installed release preserves sources, unpublished edits and current restrictions |
| 2 | Recover email delivery after downtime | The consuming application retains exact source bytes, instructions and the submission ID before sending; bounded recovery uses that ID and exposes the eventual job/outcome without asking for another email |
| 3 | Demonstrate knowledge quality and recovery | Run the existing thirty cases plus representative email/PDF/OCR inputs through the configured worker and consumer; inspect pages, answers and citations, measure the intended operating size and complete a populated restore |
| 4 | Ship focused browser permissions | A person can find a wiki, share/change/revoke access and understand pending or uncertain changes; secure sessions, current authorization and accessible interaction are release requirements |

Delivery recovery belongs in the existing consumer and its scheduler. Before a
job is observed, replay only unchanged input with the same ID. Once a job is
known, inspect/reconcile it; never automatically rerun failed, interrupted or
uncertain model work, or repeat the whole email task's other side effects.
Retain visible failures and require explicit action when recovery is paused.
Older metadata-only receipts cannot reconstruct missing source bytes and must
not be silently replayed with regenerated content. See
[submission recovery](clients.md#recoverable-submissions-and-jobs).

Browser acceptance starts with named sharing and revocation. The implemented
copy/archive, device, agent and instance-owner controls remain available; they
do not enlarge the immediate acceptance scope for clients that do not use them.
Further administration features need demonstrated demand. The existing CLI
remains supported, and provider-wide account administration stays in the identity
provider. Qualify the implemented logout integration with its configured issuer;
local suspension and current-grant enforcement remain required independently.

The optional writable inbox is outside this immediate sequence. Introduce it
only for a named workflow that existing email/API intake cannot serve; consent,
storage limits, duplicate handling and recovery are prerequisites to exposing it.

Keep facts in their granted wikis and installation/quality receipts private. Use
the existing acceptance specification and a small results record; a new evaluation
framework, profile system, maintenance linter or queue service is unnecessary.
Qualify the actual consumer being adopted; unrelated Windows/SMB adoption does
not block an agent workflow. Search remains lexical and scores from separate
wiki indexes are not calibrated global relevance. Improve retrieval or context
selection only after recorded misses show the smallest useful change.

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
| Browser access management | Dedicated OIDC sessions, wiki list/create, named sharing, role changes/removal, pending retries, durable receipts and grant activity; see [web scope](web-ui.md) |
| Local copies | User/wiki copy settings, scoped Git credentials and current permission checks before synchronization |
| Device sync | Single-copy and batch sync, opt-in discovery, exclusions, per-copy locks and structured failures |
| Network drive | SMB drive per person following grants and copy policy; writers edit Markdown pages, published as attributed, merged commits with conflict copies; originals read-only; revocable device credentials; optional per-person writable inbox |
| Agent access | Separately revocable keys or explicit Pocket ID workload bindings; read/contribute ceiling and no managed Git copies |
| Offboarding | Local suspension blocks human sessions and owned agents; backend revocation remains pending until confirmed |
| Archival | Reads and evidence retained, writes stopped, unfinished ingests interrupted and Git credentials revoked |
| Intake support | Text/HTML, PDF and image OCR, email bodies and attachments; extraction preview; up to 20 source parts in one replayable HTTP submission; guarded undo of the caller's latest unmerged ingest |

The source now includes the small correctness foundation; installation acceptance
and populated quality measurements remain separate:

- Publication validates preserved bytes and new evidence citations in the final
  tree. Factual support still requires the quality evaluation below.
- Submission IDs bind retries to one job. Candidate commits are journaled before
  push; uncertain outcomes reconcile against accepted history when queried.
- Authorized job listing and bounded intake use the existing application/state.
- Drive pages publish after independent quiet periods. Drive status includes
  projected revisions, caller-specific publication jobs and page conflict copies.
  Missing view metadata triggers manual recovery copies; writable views and their
  base metadata remain part of the backup contract. See [network drive](drive.md).
- Search fetches Git and uses a rebuildable, stemmed SQLite FTS5 index at that
  accepted revision, across all readable wikis by default; failure falls back to
  a content scan. Reads accept line ranges, and each wiki's `index.md` overview
  describes it. Page/reference and original reads pin a full accepted revision
  while enforcing current access. Server-side Git packing is bounded to stay
  within the example container memory limits.
- The bundled worker selects complete relevant pages within its default
  256,000-byte budget and rejects replacement of omitted existing files. Complete
  sources must fit. External workers need their own size/attempt qualification.

Implementation anchors: [publication and retrieval](../src/wiki_hub/service.py),
[Git operations](../src/wiki_hub/gitstore.py), [job journal](../src/wiki_hub/state.py)
and [bundled worker](../src/wiki_hub/runner.py). The milestones below retain the
contracts and required evidence; implemented source alone does not close them.

## Outstanding work after the source audit

The recoverable journal, revision-pinned evidence, FTS5 search, whole-page context
selection, native workload bindings and editable network drive are implemented.
They are not a list of features still to build. The [source audit](audit.md)
records the correctness fixes and their regression coverage separately from a
release or installation claim.

| Work | What remains |
|---|---|
| Web installation acceptance | Configure the separate browser client and read-only directory key; qualify real My Apps discovery, pre-login recipient selection, delegated sharing and existing-session revocation; [checklist](web-ui.md#implementation-status-and-remaining-work) |
| Browser control acceptance | Logout hooks, member-label refresh, settings, agents, devices and owner administration are implemented; qualify them on the installed identity and file server. Invitations/groups/expiry remain deferred |
| Drive recovery and diagnostics | Implementation is complete; qualify recovery of unpublished edits and base metadata, per-page quiet periods and caller-owned diagnostics on the installed release |
| Optional writable inbox | Deferred until a named need is unmet by email/API intake. Consent, storage bounds and recovery must precede exposure; provisioning folders alone is insufficient |
| Email delivery recovery | Qualify the consuming application through temporary downtime and restart, lost acknowledgement, accepted-job observation and paused recovery; no whole-agent or failed-model retry |
| Knowledge quality and operating size | Run the existing 30-case specification through the configured worker and consumer; record retrieval, attribution, OCR accuracy and resource/latency measurements |
| Installed clients | Accept each actual Windows, second-person and hosted-client flow, including conflicts, sleep/resume and connected revocation. Source tests do not establish device adoption |
| Delivery and recovery | Track each source release, public export and running image separately; confirm installed client identities and rehearse a populated restore including unpublished drive edits and current restrictions |

Office conversion, voice transcription, self-service/group enrollment and new
retrieval infrastructure remain deliberate deferrals. There is no general
permanent-delete API: archive retains evidence, and denying local copies removes
a wiki from the drive. Disposal of unregistered migration targets belongs in
private operator tooling with its own identity and registration checks.

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

The existing SQLite jobs, Git job trailers and retained worktrees distinguish
model work, publication
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

**Evidence contract.** Revision-pinned page/reference and original reads exist
in API, MCP and CLI surfaces where applicable. Resolve full commit
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

**Quality gate.** Execute the existing 30-case specification:
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
download capacity are separate facts. PDF/image OCR is implemented and its
derived text is labelled for verification against the original. Qualify its
configured languages and source quality separately; unsupported formats still
need explicit conversion.

The bundled worker uses bounded relevant-page selection when a whole wiki does
not fit. Qualify that selection before declaring a collection writable. Supply complete target
pages before full-file replacement; never silently truncate a source or replace
a page from a partial view. Oversized sources stay explicit failures requiring
reviewed splitting/conversion. External workers must prove equivalent preservation
and resource bounds without inheriting an assumed 256 KB limit.

The revision-keyed, rebuildable SQLite FTS5 index supplies
[ranking and snippets](https://sqlite.org/fts5.html).
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

For the network drive, accept on each installed client: connect through the
real file server, a grant appearing and an unshare disappearing without
reconnecting, read-only originals, a page edited in a common editor publishing
as the person, a deliberate conflict, sleep/resume and the private route. The
inbox additionally needs its intake consumer and storage limits before it is
offered.

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
