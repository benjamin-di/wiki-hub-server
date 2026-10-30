# Operations and recovery

Run one application worker. Its SQLite job journal records accepted revisions
and marks unfinished jobs interrupted after a restart. Inspect failed jobs
before resubmitting; the service does not silently rerun model work.

`WIKI_STATE_DIR/indexes` is a derived search cache and can be omitted from backups.
Deleting it never deletes accepted Git/LFS content, grants or jobs. Queries
rebuild it from accepted Git. Index failure falls back to a scan at that revision;
the scan approximates FTS5 token matching and ranking, so result ordering can differ.

A push reply or the subsequent journal update can be lost. The job records its
candidate before pushing; restart changes `publishing` jobs to `uncertain`.
Reading a job reconciles that exact commit against accepted remote history,
including later commits, without rerunning a model or publishing anything.
Unavailable remote history or a candidate not yet observed leaves the outcome
uncertain; only an explicit Git rejection establishes a failed publication.
Legacy failed jobs
without candidate metadata still require inspection of their Git job trailers
and retained worktrees before resubmission. `wiki jobs WIKI` discovers retained
jobs, including rollback. See [submission recovery](clients.md#recoverable-submissions-and-jobs).

Back up application state, working copies and Forgejo configuration/database and
repositories/LFS. In the example stack these are the `app`, `workspaces`,
`git-data` and `git-config` volumes. Stop writes for a consistent snapshot,
restart the services, then encrypt and upload that copy off-site using your
normal backup tooling. If consistency requires stopping the application for
the copy, API/MCP reads and ingestion are unavailable for that entire interval.
Measure the pause with populated state; a cold copy is not necessarily brief.

The example also owns its dedicated `identity` volume;
a shared identity provider needs independent backup ownership. Wiki backup or
restore must never stop or overwrite shared production identity. Test combined
recovery using an isolated restored provider. Keep identity encryption and backup
decryption keys accessible separately from the server.

When the network drive is enabled, include writers' editable views and parked
pages together with their base metadata in application state. Stop SMB writes
as well, or snapshot both stores consistently; see the [drive backup and recovery
guidance](drive.md#configuration). Writable views can contain unpublished work
and must not be discarded as a cache.
Use the [saved-page checks](drive.md#check-a-saved-page) to distinguish a
local save, a projected view and an accepted Git publication before recovery.

Before relying on real knowledge, restore a populated backup into separate
volumes. Compare file hashes, check each SQLite database, run `git fsck`, verify
original LFS document hashes and sizes, and start the recovered services on a
private network. Test restored access grants, authenticated API/MCP retrieval
and original downloads. Do not point a recovery rehearsal at live volumes or
publish its ports. A successful upload or an empty database is insufficient.

Before exposing a recovered service, check suspensions, revoked agents and pending
permission changes against retained operations records, including changes made
since the snapshot. Reapply later restrictions through the control plane and
confirm Git credential revocation. A restored old grant is not evidence of current
authorization. Keep the recovered service closed while access is uncertain.

SQLite checks should use disposable copies of the database and its committed
WAL. Opening a saved WAL database with `mode=ro` can still alter sidecar files;
`immutable=1` can ignore required WAL data. Preserve the recovered input while
checking a disposable copy. Keep the retained restore report private.

Once a true off-site restore succeeds, schedule nightly backups, failure
alerts and periodic replacement-service recovery tests with your scheduler.
Retention and cloud-version lifecycle rules belong in your deployment. A
restricted cloud key and retention window can limit damage but are not an
absolute guarantee against deletion. Previously downloaded copies cannot be
remotely erased.

For an upgrade, retain the exact old image/configuration and a matching state
snapshot. Rehearse schema changes on a copy. An image rollback alone does not
roll back its database. Keep an existing wiki system separate until the new
system's pilot and migration/recovery plan are accepted.

## Client recovery after downtime

A stopped application cannot accept a submission into its journal. Consumers
retain original source bytes, filing instructions and the submission ID until
they confirm a job outcome. After service recovery, inspect authorized jobs or
explicitly replay the unchanged submission with the same ID. A lost response
does not establish that nothing was accepted; using a new ID can duplicate work.
Failed, interrupted and uncertain jobs require inspection and explicit action,
not an automatic model retry. See [submission recovery](clients.md#recoverable-submissions-and-jobs).

## Upgrade acceptance

Track source qualification, image publication, deployment and client acceptance
separately. A successful CI job does not prove that the expected image exists
or that a production instance runs it. Verify the image's immutable digest and
source/version identity before pinning it, and retain the previous configuration
and state recovery point.

After deployment, verify readiness and the running identity, then exercise an
existing sign-in, a known page, original download and controlled ingest. Check
actual grants and an installed copy's synchronization. Test permission changes
against existing API/MCP sessions and Git/LFS credentials. Preserve current work
and investigate failures before changing further components.

Use `/health` for process availability; it reports no dependency or knowledge
readiness. Follow it with authenticated known-page and original retrieval, and
inspect pending permission changes and jobs. Existing operator checks and a
scheduler are sufficient; health probes should not invoke a paid model.
The stateless `/mcp` endpoint's HEAD/GET responses are not OAuth checks; use the
SDK or the documented
[authentication probe](clients.md#stateless-mcp-authentication-probes). Keep
platform and hosted-client checks explicit. Record the evidence privately, and
publish reusable instructions and synthetic examples only.

## Network drive upgrade checks

Record the Hub source revision and immutable image digest separately from the
file server's configuration and the installed client version. Include editable
views and their base metadata in the recovery point before an upgrade.

1. With the drive enabled, fetch `/drive/windows` and
   `/drive/windows?action=disconnect`. Both should return HTTP 200 and a
   PowerShell script using the configured public Hub address and the requested
   action. Unexpanded template placeholders indicate an incorrect response.
2. From an actual Windows computer on the LAN or private route, approve the
   browser sign-in and verify the Explorer mapping and saved credential. Check
   the exact wiki list against that person's grants and local-copy settings.
3. Check read-only originals and a reader's pages. With a writer on a disposable
   acceptance wiki, save a Markdown edit, inspect its attributed publication job,
   and exercise concurrent edits and conflict recovery. Check a save during
   publication and reconnection after sleep.
4. Revoke access while the drive is connected and confirm the file server denies
   further access. Disconnect and repeat any pending revocation until the server
   confirms it. Retain the acceptance results privately.

HTTP 200 from the helper proves script delivery. It does not prove Windows
Credential Manager, browser approval, SMB authentication or connected revocation
on an installed computer. Keep those acceptance results explicit. See the
[drive guide](drive.md#what-people-and-administrators-do) for the user commands.
