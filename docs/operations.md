# Operations and recovery

Run one application worker. Its SQLite job journal records accepted revisions
and marks unfinished jobs interrupted after a restart. Inspect failed jobs
before resubmitting; the service does not silently rerun model work.

`WIKI_STATE_DIR/indexes` is a derived search cache and can be omitted from backups.
Deleting it never deletes accepted Git/LFS content, grants or jobs. Queries
rebuild it from accepted Git; index failure falls back to a correct content scan.

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
`git-data` and `git-config` volumes. Stop Wiki writes briefly for a consistent
snapshot, restart the services, then encrypt and upload that copy off-site using
your normal backup tooling. The example also owns its dedicated `identity` volume;
a shared identity provider needs independent backup ownership. Wiki backup or
restore must never stop or overwrite shared production identity. Test combined
recovery using an isolated restored provider. Keep identity encryption and backup
decryption keys accessible separately from the server.

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
