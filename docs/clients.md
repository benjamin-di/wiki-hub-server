# Clients

## CLI and local MCP

Install the CLI from the selected release checkout with `uv tool install .`.
Run `wiki --server https://wiki.example.com login`, approve the fresh device
code with your passkey and keep the terminal open until it reports signed in.
Subsequent commands use that sign-in and refresh it when needed.
Commands sharing a configuration directory coordinate credential updates and
reuse refreshed sign-ins across processes. If another sign-in operation remains
busy, the command reports a bounded wait failure and can be retried.

A local MCP client starts the authenticated bridge:

```json
{
  "mcpServers": {
    "wiki-hub": {"command": "wiki", "args": ["mcp"]}
  }
}
```

Ensure the desktop application's environment can find the installed executable.
On Windows, use its full path if needed and store clones in an ordinary Windows
folder outside OneDrive. Windows uses Credential Manager and does not fall back
to a plaintext token file. A headless Unix client can use a private configuration
directory. WSL and Windows can keep separate clones and sign-ins.

## Device sync

Git and Git LFS must be installed. Clone installs repository-local LFS filters
without downloading every original. Sync also repairs missing local LFS setup
for existing copies, keeping binary originals in LFS. Download individual sources
with `wiki docs get` or use Git LFS explicitly when their bytes are needed locally.

```sh
wiki sync ~/Wikis/handbook
wiki sync --all --root ~/Wikis --json
```

The batch command examines direct, non-symlink children of the explicit root.
Only repositories marked by `wiki clone` are managed; ordinary repositories are
left to their existing Git workflow. A configured default server is unnecessary:
each copy identifies its own server. Zero copies reports an empty result. Missing
credentials, incomplete markers, denied policy, conflicts and busy locks produce
per-copy failures and a nonzero exit status while other copies are still checked.
Without additional options, no new wikis are cloned.

To opt this device into discovery, sign in to the desired server, then run:

```sh
wiki sync --all --discover --root ~/Wikis --json
```

Discovery lists the currently accessible wikis and checks their current copy
eligibility before requesting each missing clone. Readers and contributors get
read-only credentials; writers and admins get writable credentials. Disabled
copy policies are reported as skipped. Unavailable permissions and occupied
destinations are reported as failures, while other copies are still checked.
Existing folders and symlinks are never adopted or replaced. Existing managed
metadata prevents duplicate clones even when a copy uses a different folder
name. Moving a copy does not transfer its saved credential; repair it explicitly.
Discovery requires a configured server or `--server URL`, and grants no access.
Keep the root's discovery lock file; concurrent discovery stops without cloning.

The current copy permission is checked before staging edits. Run against a server
supporting `/api/wikis/WIKI/copy-access`; an older/unavailable server stops sync
without committing local work. A deliberately read-only copy is never upgraded by
this check. A writable copy temporarily made read-only by permissions or archival
renews its credential when writing becomes allowed again. Archive/reopen revokes
existing Git credentials; use `wiki sync DIRECTORY --refresh-credential` if a
credential was revoked without a change in effective write permission. This
renews the credential without rebuilding the checkout or discarding edits.
Copies stay on `main` tracking `origin/main`; changing the remote or leaving
a merge, rebase or another Git operation unfinished stops synchronization.

For repositories retaining application work, `.autocommit-pathspec` accepts one
Git pathspec per line (blank lines and `#` comments are ignored), for example:

```text
.
:(exclude,glob)*.py
```

Excluded work remains local. Already staged excluded changes must be unstaged
or committed deliberately before sync; they are never folded into an automatic
wiki commit. An empty path list stops sync. Git still refuses a merge that would
overwrite held-back work. Offline edits remain on disk until a later sync works.

Existing schedulers may invoke either command. They must preserve its exit
status, and their generic Git path must skip managed copies. The OS lock covers
the whole operation, including permission checks, and releases when the process
exits. Keep its file in Git metadata; deleting it is not a way to clear a lock.

A physical checkout has one executor. From WSL, pass a Windows checkout to the
native Windows `wiki.exe` with a translated Windows path. That process uses the
Windows credential store; do not also run Linux sync against the same checkout.
A separate native WSL clone uses its own Linux sign-in instead.

## Text and original evidence

Search uses a per-wiki SQLite FTS5 index tied to the accepted Git revision.
Direct Git pushes and rollback invalidate it on the next query. It rebuilds
from Git; a damaged/unavailable index falls back to scanning accepted content
at the reported revision. Scores rank lexical relevance, not factual confidence.

Search results carry the accepted Git revision. Pass its full hash to
`wiki read WIKI PATH --revision COMMIT` or the MCP `wiki_read` tool's `revision`
argument to read the same evidence after later edits or rollback. Original
downloads support `wiki docs get WIKI raw/SOURCE --revision COMMIT --output FILE`.
Only commits in that wiki's accepted main history are readable; unpublished
worktrees and other wikis are excluded. Historical reads check current grants. The CLI refuses a pinned page or original
when the server does not confirm the exact requested revision, including older
servers that ignore the query. No original is saved from an unconfirmed reply.

MCP search covers Markdown and text under `wiki/` and `reference/`. Text reads
are limited to two million characters. A large indexed page can be searched for
passages; a large static JSON/CSV capture outside that index needs an authorized
local copy for analysis. Retaining a historical audit capture preserves its
evidence without making its full dataset part of ordinary model context.
Raw originals remain available on demand through `wiki docs get`. Mutable
application state belongs to its application, independently of these retained
sources and accepted reports.

## Recoverable submissions and jobs

`wiki contribute` saves and prints a submission ID before sending the request.
After a lost reply, repeat the exact command with `--submission-id ID`. The
private configuration directory retains submission metadata without source text,
filing instructions or credentials. The CLI uses `POST /api/wikis/WIKI/submissions`, which requires the retained ID.
An older server refuses this route before accepting a source; the CLI never
falls back to legacy ingestion. The existing ingestion API and MCP accept the
same optional `submission_id`; tool callers should retain their chosen ID and
check that the discovered tool supports it before calling.
Reusing an ID with different source bytes, filename or instructions is refused.
A deliberate new ingestion can use a new ID. A failed/interrupted job is not
automatically rerun by reusing its ID.

Use `wiki jobs WIKI`, optionally `--status STATUS --limit NUMBER --before CURSOR`,
or MCP `wiki_jobs` to find retained jobs when a response ID was lost. Results
include `next_before` for pagination and remain subject to current wiki access.
They expose no private source paths or provider diagnostics.

`publishing` means the final candidate commit is durably recorded and a push is
in progress. A lost result or restart leaves it `uncertain`. Reading that job
observes accepted remote history: an accepted candidate becomes `completed`.
An explicit Git rejection makes the job `failed`; an absent candidate or
unavailable backend leaves it `uncertain`, because remote processing may still
be finishing. Later accepted commits do not hide the result.
Observation never reruns a model or republishes work. Rollback also records a job.
Inspect retained work before deliberately starting a new operation.

The application bounds pending jobs with `WIKI_MAX_PENDING_JOBS` (default 32).
A full queue returns HTTP 429 before accepting another job. Reusing a submission
ID still finds its existing job while the queue is full.

## Hosted Claude and ChatGPT

Add a separate custom connector/app pointing to
`https://wiki.example.com/mcp`. Select OAuth and use a public client with PKCE,
no client secret and scopes `openid offline_access wiki:access`.

| Client | Public client ID in the example setup | Exact callback |
|---|---|---|
| Claude | `wiki-claude` | `https://claude.ai/api/mcp/auth_callback` |
| ChatGPT | `wiki-chatgpt` | `https://chatgpt.com/connector_platform_oauth_redirect` |

When configuring ChatGPT manually, use token authentication method `none`.
The example provider registers static public clients rather than depending on
automatic registration support. Hosted client availability depends on your app
account and its current custom-connector settings.

Complete consent in the browser that started the connection. For a passkey on
another device, use the identity provider's native cross-device sign-in; copying
an app's raw authorization URL between browsers can break its OAuth callback.
Keep existing connectors separate while trying a new deployment.

## Users and wiki permissions

Create human accounts and manage passkeys, account disablement and optional
service-admission groups in the identity provider. Wiki Hub uses each person's
immutable subject; an email, display name or group membership is not a wiki grant.
The configured Wiki owner creates wikis. A wiki's owner and explicitly granted
admins manage its shares.

The recipient obtains their subject with `wiki whoami`. The configured owner
creates the wiki; its owner or an explicitly granted admin then shares it:

```sh
wiki create handbook --title "Handbook"
wiki share handbook RECIPIENT_SUBJECT --role read
wiki access handbook
wiki unshare handbook RECIPIENT_SUBJECT
```

Choose `read`, `contribute`, `write` or `admin` explicitly. Contributors can read
and submit sources; ordinary accepted ingests save automatically. Managed Git
copies additionally require the user and wiki copy settings. Inspect the caller's
effective permissions with `wiki whoami --access`.

For immediate offboarding, the configured owner uses `wiki user suspend SUBJECT`
and checks `wiki user status SUBJECT`, alongside the provider's account/group
changes. Local suspension also blocks owned agents and revokes managed Git
credentials. Pending backend revocation is incomplete and needs an explicit
retry. Provider removal alone can leave issued tokens valid until expiry.
Resume is explicit with `wiki user resume SUBJECT`; old provisioning settings
must not silently restore access. Treat independent workload applications and
previously downloaded copies separately.

## Scoped agents

The configured owner registers an independent agent, then explicitly grants it
one wiki:

```sh
wiki agent add source-filer --directory ./source-filer-config
wiki share starter agent:RETURNED_ID --role contribute
```

Set `WIKI_CONFIG_DIR` to that private directory for the agent's `wiki mcp` process.
Do not give it the human sign-in directory. Agent grants are capped by the
owner's current access and can be revoked with `wiki agent revoke ID`.

Run `wiki whoami --access` with the same `WIKI_CONFIG_DIR` as a workflow to see
the identity it actually uses and its current per-wiki permissions. The report
includes archived wikis the caller may read, copy eligibility and the permitted
roles. An archived wiki can retain `admin` while content writes and ingestion
are denied. An agent's permissions intersect its own grants with its owner's
current access; agents never receive `write`, `admin` or local-copy access.
Wikis the caller cannot read and other people's grant records are omitted.
The report describes Wiki Hub access; host files and other services have their
own permissions. It does not change grants or request Git credentials.

To replace an agent credential, create a replacement with `wiki agent add`
using a new private configuration directory. Grant only the reviewed wikis and
roles with `wiki share`, then inspect `wiki whoami --access` using that new
directory. Switch the workflow to the new configuration and check it works
before revoking the old agent with `wiki agent revoke OLD_ID`. The replacement
starts without grants and keeps its own identity; no saved manifest restores
the old agent's access. If the old credential must stop immediately, revoke it
first and accept the interruption while replacing it.

## Pocket ID workloads

A service that already has a Pocket ID workload identity can use it directly.
The configured Wiki owner binds that application without creating another key:

```sh
wiki agent add source-filer --workload-client source-filer
wiki share handbook agent:RETURNED_ID --role contribute
wiki agent list
wiki agent revoke agent:RETURNED_ID
```

Use an existing dedicated confidential workload application, not one of the
human CLI/desktop clients. Keep its private key in the workload's protected
persistent storage and register only its public key with Pocket ID. Enable
replay protection. Grant the application Client Access to the Wiki resource
and `wiki:access`, with a maximum 15-minute access-token lifetime. Do not add
user-delegated access or human callbacks to that application. The workload's
standard OAuth client requests the Wiki resource with `client_credentials`
and `private_key_jwt`, then calls the ordinary `/mcp` or `/api/` endpoints.
Wiki Hub verifies the access token; it never receives the workload private key
or a provider administrator credential.

The command uses a distinct enrollment endpoint and issues no `whagent_` token,
configuration directory or Git credential. It cannot silently create a local
key on an older server. The binding reserves the configured issuer and client
ID for that workload: its signed subject must be Pocket ID's canonical
`client-CLIENT_ID`. Unknown machines and human tokens from a reserved workload
client are refused. Signed claims cannot select a local agent or its owner.
Regular human sign-ins continue to use their own Wiki grants.

Workloads start with no wikis and use the same read/contribute ceiling, current
owner grants and publication checks as local-key agents. The server preserves
the signed identity separately from the local authorization actor. A workload's
`GET /api/me/access` reports `credential_type: workload` and the bound issuer,
client ID and signed subject alongside its actual Wiki permissions. No access
token appears in this response.

Revoking the local agent blocks new operations in already connected sessions
and prevents queued ingests from publishing. An already admitted publication finishes
before revocation returns. Provider key/API revocation stops new tokens; existing
tokens remain valid until expiry unless the local agent is also revoked.
Existing downloaded data cannot be recalled. A revoked client binding remains
reserved and is never re-enrolled automatically; an independently reviewed new
application is required to replace it. Provider key rotation for an active
application keeps the same local binding. Back up the current catalog and
permissions; restoring stale state must not restore revoked access.

Human offboarding still includes local Wiki suspension and explicit treatment
of their independent workload applications. Removing a person's Pocket group
membership alone does not withdraw an application's Client Access grant or
persistent Git credentials. Group-to-wiki synchronization is not required for
these explicit permissions.

### Stateless MCP authentication probes

The stateless `/mcp` endpoint accepts POST. HEAD and GET can return `405` without
an OAuth challenge, so they do not establish whether workload authentication
works. A client's token helper can use a harmless MCP `ping` POST to obtain the
`401` Bearer challenge and protected-resource metadata through its OAuth SDK:

```json
{"jsonrpc":"2.0","id":"auth-probe","method":"ping"}
```

This probe does not initialize a session or call a tool. Keep the selected Wiki
resource and `wiki:access` scope; do not substitute a gateway's resource token.
A successful token exchange or ping does not establish per-wiki access. Complete
acceptance with an actual wiki listing, page read and permission-revocation test.
