# Operations and recovery

Run one application worker. Its SQLite job journal records accepted revisions
and marks unfinished jobs interrupted after a restart. Inspect failed jobs
before resubmitting; the service does not silently rerun model work.

Back up application state, working copies, Forgejo configuration/database,
repositories/LFS and identity state together. In the example stack these are
the `app`, `workspaces`, `git-data`, `git-config` and `identity` volumes. Stop
writes briefly for a consistent snapshot, restart the services, then encrypt
and upload that copy off-site using your normal backup tooling. Keep identity
encryption and backup decryption keys accessible separately from the server.

Before relying on real knowledge, restore a populated backup into separate
volumes. Compare file hashes, check each SQLite database, run `git fsck`, verify
original LFS document hashes and sizes, and start the recovered services on a
private network. Test restored access grants, authenticated API/MCP retrieval
and original downloads. Do not point a recovery rehearsal at live volumes or
publish its ports. A successful upload or an empty database is insufficient.

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
