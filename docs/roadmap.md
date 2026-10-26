# Roadmap

This roadmap covers the reusable product. Deployment inventories, account
bindings, migration manifests and retirement instructions belong in an
operator's private infrastructure repository. Planned features below are not
commands or guarantees in the current release.

## Design and scope

The immediate goal is reliable, source-backed knowledge that people and scoped
agents can read, update and synchronize. Keep the existing application, SQLite,
Forgejo 17, Git LFS and external identity provider. Improve the workflows before
adding services or more policy machinery.

| Component | Owns |
|---|---|
| External identity provider | Human accounts, passkeys, sign-in, OAuth clients and token issuance |
| Wiki Hub | Wikis, ownership, per-wiki grants, agent credentials, copy settings and ingestion |
| Forgejo | Accepted Git history, repositories and LFS objects; projected Git access |
| Device CLI | Local copies and explicit synchronization through Git |
| Applications consuming knowledge | Their executable releases, calculations, artifacts and mutable operational state |

A shared identity provider does not need a second wiki-specific directory.
An optional gateway may restrict which service a caller can reach; Wiki Hub
still decides which wiki that caller can use. Do not replace an independently
identified agent with its owner's broader backend credential.

Use the configured issuer and immutable subject, never an email or display
name, as the identity boundary. Preserve issuer, subjects, keys and client
registrations when changing provider ownership or hosting. Provider admin keys
belong to identity operations, not Wiki Hub. Wiki deploys and backups must not
stop or restore a shared production identity service. Test combined recovery
with a disposable independently recovered provider.

## Current baseline

The current implementation provides private repositories, Git LFS, OAuth, CLI, remote/local
MCP, source ingestion, lexical search, history and rollback. Explicit
read/contribute/write/admin grants, copy settings and separately revocable agent
keys are implemented. The configured owner creates wikis and registers agents.

`wiki sync DIRECTORY` synchronizes one existing copy: commit writable
edits, fetch, merge and push; read-only copies fetch and fast-forward. Conflicts
stop the operation. Batch synchronization with explicit roots, current copy
permission checks, path exclusions and per-copy OS locks is implemented in
0.5.0. Opt-in catalog discovery is also implemented; legacy migration is
operator-owned. Human clients using the same person's OAuth login share
that person's grants. A connection's name does not create an agent principal.

Human account creation/disablement uses the identity provider. `wiki user set`
controls copy settings. Local `wiki user suspend/status/resume` operations also
block current human sessions and owned agents and revoke managed Git credentials.
Incomplete backend changes remain pending across restarts. Disabling an identity
or gateway route alone does not immediately suspend Wiki access; compose the
separate operations in the operator's offboarding procedure.

Wiki admins can archive and reopen a wiki. Archival preserves reads and evidence,
stops new content writes, interrupts unfinished ingests and revokes managed Git
credentials. Archived wikis are omitted from the default catalog. Incomplete
backend updates remain read-only until an explicit retry succeeds. Reopening
preserves grants and copy settings without reviving interrupted work.

## Knowledge and application boundaries

Keep explanations, decisions, curated records, original evidence and selected
accepted reports in a wiki. A generated report can be valuable evidence; an
extension or an `output/` directory alone is not a reason to discard it.

Executable application code, tests and dependency files belong to reviewed
application releases. Repeated run outputs, calculation data, current-report
pointers, delivery receipts, cursors and retry state belong to their application.
Use existing application storage and deployment mechanisms first. Moving a
workflow does not require a new microservice, database or artifact platform.
Documentation code examples and code retained as evidence can remain knowledge.

An application using wiki originals records exact revisions and byte hashes in
its run inputs. The wiki may link to the current application report and retain
accepted snapshots. The linked artifact needs its own access control. Preserve
manual data-entry workflows and backups when moving application-owned records.
Separate only workflows affected by a particular migration; independent wikis
need not wait for a complete application reorganization.

## Step 1: finish everyday sync

`wiki sync --all --root DIRECTORY` scans existing managed clones and their Git
metadata, with structured results, permission checks and per-copy locks. Keep
the explicitly configured root; no daemon or parallel inventory database is
needed. Add persistent registration only if actual use
requires copies outside that root or relocation management.

- Use per-copy locks, meaningful exit status and a result for each copy. One
  failure must not hide other results or report the whole invocation as healthy.
- Preserve offline edits; stop on conflicts without force pushes or model
  conflict resolution. Respect explicit exclusions while mixed repositories
  remain. An operator's generic Git script must skip managed copies.
- Distinguish readers/contributors from writers using effective role and copy
  eligibility returned by the API. Discovery never grants access.
- Keep one executor for a physical checkout. A shared Windows/WSL copy uses the
  native Windows CLI and credential store, or WSL uses a separate signed-in
  clone. Do not run both Git implementations concurrently against one copy.
- Integrate existing scheduler/wrapper examples with these commands. Explicit
  `wiki sync --all --discover --root DIRECTORY` creates missing eligible copies
  from the signed-in server. Keep discovery opt-in on each device; it never
  grants access or replaces existing folders.

Acceptance: two devices exchange edits in both directions; offline work survives;
read-only edits remain local; revocation stops future managed fetches; overlapping
runs and a real conflict preserve work and report failure. Test actual supported
devices. Claim complete automatic-sync parity only after discovery also passes.

## Step 2: make access operable as use expands

Keep human enrollment in the identity provider and wiki sharing in Wiki Hub.
Use explicit grants before adding group synchronization or a complete account
administration CLI.

Current independent-agent commands remain the starting point:

```sh
wiki agent add source-filer --directory ./source-filer-config
wiki share handbook agent:RETURNED_ID --role read
wiki access handbook
wiki agent revoke agent:RETURNED_ID
```

Agents start with no grants and are capped by their owner's access. `contribute`
allows read and automatic source ingestion; it does not allow arbitrary editing,
Git credentials or administration. Wire one real workflow at a time with its own
key and required wikis. Remove alternate broad mounts, old MCP credentials and
human tokens from deliberately restricted runtimes. A trusted operator shell
with wider access is not restricted by its MCP grant alone.

The implemented Wiki suspension operation denies their API/MCP operations and
agent publication, revokes managed Git credentials and reports incomplete backend
revocation. Compose it with provider/gateway offboarding in a private operator
runbook; do not build an identity synchronization bus. Suspension covers wiki
owners. Resume only through an explicit action, never a stale provisioning manifest.

Use `wiki whoami --access` to inspect the actual configured principal, current
per-wiki permissions and copy eligibility. It uses the same permission checks
as content operations, includes readable archived wikis and never lists another
person's grants. Replace credentials through the documented new-agent,
reviewed-grant and old-agent revocation procedure in [Clients](clients.md).
Verify two disjoint agents and revocation in already connected sessions.
Gateway workload keys and Wiki agent keys remain
separate credentials with separate audiences and permissions unless an explicit
delegation contract is implemented and tested.

## Step 3: expand from observed needs

Support large collections after measuring their ingestion and retrieval needs.
The standalone worker's whole-wiki context budget is not proof of support; use
a bounded context selection and preserve the existing runner interface. Complex
application integrations should have their own acceptance checks, rather than
blocking ordinary knowledge workflows.

Keep operational backup/recovery and client compatibility documented. Retention,
legacy data transfer and infrastructure retirement are operator responsibilities;
they are not a new import product or a public migration API.

Group administration, background watchers, critical-value approval gates and
new retrieval/storage platforms are deferred
until a demonstrated need. Keep public examples synthetic and deployment details
private. Judge completion by a working user workflow and recoverable data,
rather than by the number of planned subsystems.
