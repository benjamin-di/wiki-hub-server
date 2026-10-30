# Network drive

People can map one drive that contains every wiki they may currently read. Wiki
pages are editable for people who may write; originals and references stay
read-only, and nothing on the drive is ever ingested. The drive works with the
operating system's own SMB client and needs no sync software. It is intended for
a LAN or private network route; never expose SMB to the internet.

## What people and administrators do

On Windows, nothing needs installing. A person pastes one line into PowerShell
(no administrator rights needed):

```powershell
irm https://wiki.example.com/drive/windows | iex
```

Replace `https://wiki.example.com` with your Wiki Hub address. The computer must
also reach the file server over a LAN or private route; reaching the HTTPS
sign-in page alone does not establish an SMB connection.

The server publishes this script at `/drive/windows` without sign-in; it holds
no secrets. It opens the browser to approve the computer with the person's usual
sign-in, then issues a credential for that device. The credential goes into
Credential Manager without being displayed. The script then maps the drive (`W:`
or the next free letter), labels it in Explorer and opens it. Running the line
again repairs the connection: it issues a new login and revokes this computer's
previous one, matched by computer name. The script runs on Windows PowerShell
5.1 and PowerShell 7.

To remove the mapping and saved login and revoke this computer's devices:

```powershell
irm 'https://wiki.example.com/drive/windows?action=disconnect' | iex
```

If server revocation is pending, run disconnect again to finish. Removing a
local mapping alone does not confirm that the server revoked its credential.

With the CLI, `wiki drive connect` does the same, or the CLI offers it after
`wiki login` on Windows. `wiki drive status` shows the wikis, whether each one's
pages are editable (`access`; `pages` is the drive's current state), page edits
not yet published, files that will not be published, devices (`--all` includes
revoked ones) and file-server state. `wiki drive disconnect` revokes the device
and removes the mapping. Other systems receive the server path and a one-time
password to save in their password manager.

For each wiki, status includes the revision currently projected on the drive,
the person's ten most recent drive publication jobs (including failures,
uncertain outcomes and their candidate or accepted revisions), and visible
conflict-copy paths. Use the job ID with `wiki status ID` for the outcome and
`wiki log WIKI` for accepted history. Publication jobs belong to the caller;
another person's pending drive work is not included.

Administrators use the existing commands. A read grant (or higher) plus both the
person's and the wiki's local-copy settings put a wiki on that person's drive; a
write or admin grant on an open (not archived) wiki makes its pages editable.
`share`, `unshare`, copy settings, `archive` and `user suspend` take effect
before they report success; a backend failure leaves the change `pending` and
denies access until repeated. `wiki drive devices --subject SUBJECT` and
`wiki drive revoke ID` let the configured owner see and revoke any device.
Agents never receive drive access.

## Connection troubleshooting

- If `/drive/windows` returns 404, ask the operator to check the running Hub
  release, the request route and the three required settings: `WIKI_DRIVE_DIR`,
  `WIKI_DRIVE_CONTROL_DIR` and `WIKI_DRIVE_SERVER`. The helper is served only
  when all three are configured.
- If browser approval works but mapping fails, check the private route to the
  file server and TCP port 445. The Wiki Hub HTTPS address and SMB server address
  can differ. `wiki drive status` reports the configured server and reachability.
- If a wiki is missing, check the person's current grant and both local-copy
  settings. A successful sign-in grants no wiki access by itself.
- If disconnect reports pending revocation, repeat it after the file server
  recovers. Operators inspect pending permission changes before reporting success.
- If recovery after view metadata loss cannot read a writer's folder, rebuilding
  waits until the folder is readable. Restore directory access and retry so the
  unpublished files can be retained before replacing the view.

Operators should follow the [drive upgrade checks](operations.md#network-drive-upgrade-checks).

## What each person can change

| Folder in a wiki | Read or contribute | Write or admin, wiki open |
|---|---|---|
| `wiki/` | Read-only | Editable Markdown pages: change, add, rename and delete `.md` files and folders |
| `raw/`, `reference/`, `output/` | Read-only | Read-only |
| The wiki folder itself | Read-only | Read-only |

Ordinary pages such as `wiki/index.md` and a human-maintained `wiki/log.md`
follow the same editing rule. Root instructions (`AGENTS.md`) and configuration
are not projected onto the drive. These are the drive's editing boundaries;
people with a write or admin grant can also edit the repository through Git.
Pocket ID establishes identity, and Wiki Hub checks its current wiki grants and
copy settings before publication.

Only Markdown pages under `wiki/` are published. Other files saved there (a PDF,
an image, an Office document) stay in that person's folder, are listed by
`wiki drive status` as not published and are never committed or ingested.
Editor temporaries (`~$…`, hidden files and folders such as `.trash`, `.tmp`,
`.bak`, swap files) are ignored. A page that cannot be published as it is (not
UTF-8, too large, or an unusable name) is listed under
`pages_needing_attention` and never blocks the person's other edits; content
the server refuses waits until the page changes (all pages in that commit, unless
the refusal names the page). Files over the upload limit are only hashed, never
read whole. Sources still enter a wiki
only through `wiki contribute` or the API.

If a folder cannot be enumerated, its existing pages are kept and the folder
appears in `pages_needing_attention`. Independent readable pages can still
publish. The next scan retries the folder and clears that diagnostic when it
can be read; an unreadable folder does not establish that its pages were deleted.

## How edits are published

Each writer has a private copy of `wiki/`. When a saved page has been unchanged
for `WIKI_DRIVE_EDIT_QUIET_SECONDS` (default 20), the application checks the
person's current write access and copy settings and publishes their changed
pages as one commit attributed to them (`Principal:` trailer), recorded as a
`drive-edit` job that `wiki jobs` and `wiki log` show and rollback can undo.

Each changed page and deletion has its own quiet period, so continuing to edit
one page does not delay another completed page. Pages ready together are batched.

Each page is merged against the version the person started from:

- If the accepted page is unchanged since then, their version is published.
- If both changed different lines, a three-way merge is published and their
  page shows the merged result.
- If both changed the same lines, or one side deleted a page the other changed,
  nothing is overwritten: their version is kept as
  `page (your edit, conflict YYYY-MM-DD HHMMSS).md` beside the page (a long
  name is shortened), which returns to the accepted version. The job records
  the conflict, and a copy is kept in the application state. Conflict copies
  are never published; merge by hand and save the page again.

Pages a writer has not changed follow accepted changes automatically; their own
unpublished changes are never overwritten, and a save made while a page is being
published is kept and published next, merged against the version it started
from. If accepted content needs a path where the person has their own file or
folder, theirs is kept beside it as `name (your file, conflict …)`. Publication
pauses for a wiki while an ingest or rollback is running there. The application
never follows links in a person's view: a page replaced by a link is neither
read nor treated as deleted, and neither is a page whose folder was replaced
by a link.

Losing write access never publishes pending edits. When pages become read-only
(a downgrade to read or contribute, or archive), everything the person saved and
did not publish (changed pages, pages needing attention and other files) is
kept in the application state (`drive-unpublished/`) first. When the wiki
leaves their drive (unshare, copies denied, suspension), their folder is first
moved out of reach. If access returns within ten minutes, for example while a
role change or setting is applied, it comes back unchanged and publishing
continues. Otherwise its unpublished files are kept in `drive-unpublished/`.
Keeping or raising write access never disturbs a writer's folder. Revoking a
person's last device keeps their unpublished files the same way, without
publishing edits still inside the quiet period. An editor that keeps an old
copy open and saves it later replaces newer accepted text in that page; history
keeps both versions.

## Check a saved page

Saving a file over SMB and accepting it into Git are separate steps. A page
still in its quiet period, waiting for another job, or needing attention may
exist only in the writer's view. Use the existing commands to check publication:

1. Run `wiki drive status`. Inspect that wiki's `unpublished_pages`,
   `pages_needing_attention`, `not_published` and `conflict_copies`, then its
   `recent_publications`. The projected `revision` identifies the drive view;
   it does not prove that every local edit is accepted.
2. Run `wiki status JOB_ID` for the relevant publication. A completed job's
   `revision` identifies its accepted Git commit. Inspect failed or uncertain
   outcomes before changing or resubmitting the page; an uncertain push may
   already be accepted. See [publication recovery](operations.md).
3. Use `wiki log WIKI` to inspect accepted history. If a conflict copy exists,
   compare it with the accepted page, merge the intended changes by hand and
   save the ordinary page again. The conflict copy itself is never published.

An empty recent-publication list is not evidence that a new save succeeded: the
list contains only the caller's ten latest drive jobs. Administrators recover
retained unpublished files from application state when access or view metadata
is lost; see [backup and recovery](#configuration).

## Behavior

| Event | Effect on the drive |
|---|---|
| Share, or a role that keeps read access | The wiki appears in that person's drive |
| Write or admin role | That wiki's pages become editable for them |
| Unshare, copies denied, suspension | The wiki leaves their drive; folders held open by clients stop resolving |
| Archive | Pages become read-only for everyone; reading continues |
| Device revoked | The account is removed and its sessions are disconnected |
| Suspension | Every device account is disabled and disconnected; resume restores them |
| Accepted ingest, rollback or drive edit | Every view follows the new revision |
| Direct Git push | Picked up by the periodic refresh (`WIKI_DRIVE_REFRESH_SECONDS`, default 60) |

Read-only files show the read-only attribute, so editors open them read-only.
Each wiki folder shows the wiki's title in Explorer through a hidden
`desktop.ini`. Files Explorer acts on when a folder opens (`desktop.ini`,
`autorun.inf`, `.lnk`, `.url`, `.scf`, library and search-connector files) are
never projected from wiki content, so a writer cannot make readers' computers
fetch icons from network paths. Originals are the verified LFS bytes, not
pointer files. Content already copied off the drive, or a file already open on a
client, cannot be recalled.

`wiki drive status` reports the file server as `available`, `lagging` (reachable
but not applying the latest account changes, which then stay pending) or
`unavailable`.

## Architecture

The application owns everything under `WIKI_DRIVE_DIR`:

- `shared/<wiki>` is the accepted tree for each wiki, never served directly. A
  new revision hard-links unchanged files from the previous tree and replaces
  it with an atomic `renameat2(RENAME_EXCHANGE)`. A build identifier in its
  `user.wiki-hub.build` xattr guards the cached manifest; after a crash it
  forces a complete rebuild instead of trusting links.
- `views/wp<n>/<wiki>` is one person's view, served by Samba. Read-only folders
  are hard links to the shared tree (files 0444, folders 0755, owned by the
  application). A writer's `wiki/` is a private copy whose folders and pages
  carry a POSIX ACL for the application and that person's group only, with a
  matching default ACL for new files. `views/wp<n>` itself admits only that
  person's group and allows no new entries.
- `lfs/<wiki>` holds each changed original, smudged and checked against its
  SHA-256 and size, then hard-linked into the trees. Objects no tree links to
  any more are deleted after each build.
- Removing access renames that wiki out of the person's view before returning,
  so a directory handle opened under the old access cannot be used for new
  lookups. Removals are applied before additions. A writer's `wiki/` is moved to
  `parked/` and restored if their write access returns within ten minutes.

A file's owner can set its ACL without privilege, so access changes need no root
helper. The filesystem must support POSIX ACLs and user extended attributes
(ext4 and XFS do by default), and everything under `WIKI_DRIVE_DIR` must be on
one filesystem. Cached manifests and view state live in the application state
directory (`drive-manifests/`), and unpublished or conflicting edits in
`drive-unpublished/`.

Samba identities are derived from the application's state and never reused:
device `wd<n>` has UID 600000+n and primary group `wp<n>` (GID 700000+n, its
person); every device is also in `wikidrive` (GID 699999). The share path uses
`%G`, the device's primary group, to select its person's view. Those numeric
ranges must be free on the file server.

The account agent ([`examples/drive/drive-agent.sh`](../examples/drive/drive-agent.sh))
runs beside smbd as root, using only BusyBox and Samba tools. It reads
`accounts.tsv` from `WIKI_DRIVE_CONTROL_DIR`, imports one-time passwords from
`secrets/<account>` (and deletes them), disables, removes and disconnects
devices, and writes `observed.tsv` into `WIKI_DRIVE_OBSERVED_DIR`, a root-owned
directory the application can read but not write, so the application user cannot
redirect the root agent's writes. After each pass it disconnects every session
that does not belong to an active, listed device. It accepts only account
names, IDs and states in the reserved ranges. The application waits up to
`WIKI_DRIVE_CONFIRM_SECONDS` for the agent's confirmation; an unconfirmed new
device is revoked and its password discarded. Device passwords are random
256-bit values. The password hash exists only in Samba's account database, so
restoring an older application backup cannot revive a revoked device.

## Configuration

| Variable | Purpose |
|---|---|
| `WIKI_DRIVE_DIR` | Shared trees and views; must support ACLs and xattrs. Enables the drive with the next two |
| `WIKI_DRIVE_CONTROL_DIR` | Written by the application, read by the agent: desired accounts and one-time passwords |
| `WIKI_DRIVE_SERVER` | Host name clients use. Give it its own DNS name so Windows can use it alongside other logins to the same host |
| `WIKI_DRIVE_OBSERVED_FILE` | The agent's `observed.tsv`, mounted read-only from its own directory (default: inside the control directory, for tests) |
| `WIKI_DRIVE_SHARE` | Share name, default `wikis` |
| `WIKI_DRIVE_DOMAIN` | Optional domain prefix for the Windows login, such as the server's NetBIOS name |
| `WIKI_DRIVE_REFRESH_SECONDS` | Periodic refresh, default 60 |
| `WIKI_DRIVE_CONFIRM_SECONDS` | Wait for agent confirmation, default 20 |
| `WIKI_DRIVE_EDIT_QUIET_SECONDS` | How long a saved page must stay unchanged before it is published, default 20 |
| `WIKI_DRIVE_EDIT_SCAN_SECONDS` | How often writers' pages are checked, default 5 |
| `WIKI_DRIVE_INBOX_SHARE` | Optional writable inbox share name, such as `inbox`. Leave unset unless the file server serves it |

Run Samba with [`examples/drive/smb.conf`](../examples/drive/smb.conf), the
`views/` directory bound read-write at `/shares/views` (the files themselves
decide what each person may change), the control directory at `/drive-control`
and the agent's report directory (`WIKI_DRIVE_OBSERVED_DIR`). Start the agent
once (`drive-agent.sh once`) before smbd so accounts exist after a container
restart, then keep `drive-agent.sh loop` running. A health check can require
`observed.tsv` to be newer than a minute. Keep Samba's private state
(`/var/lib/samba`) on persistent storage. Restrict each share with its own
`valid users`; a global `valid users` also blocks the `IPC$` access Explorer
needs to browse.

Back up the application state as usual, including `drive-unpublished/`, and
`parked/` (it holds a writer's pages for up to ten minutes during an access
change). Also back up writers' `views/wp<n>/<wiki>/wiki/` directories together
with their base metadata in the state's `drive-manifests/`. Including all of
`views/` is the simplest option; only its read-only content is rebuildable.
Preserve ACLs and extended attributes. Stop the application and file server for
the backup, or use a consistent snapshot of both drive storage and application
state. Pending, rejected or failed edits can remain unpublished indefinitely.
`shared/`, `lfs/` and the control files are derived and can be rebuilt.

If view metadata is missing or invalid during rebuild, or parked metadata is
lost, regular files are retained under `drive-unpublished/` before replacement.
Interrupted view creation is checked before staging is cleaned on startup.
Retained views have unique recovery directories with a `REASON.txt` and a
`STATE.json` containing the known base and pending edits, including deletions.
Without a trustworthy base they require manual recovery and are never
automatically published. Administrators recover these files from application
state; links are never followed. Restore current access policies before exposing
restored drive views.

Back up `inbox/` if you enable it: it holds people's pending originals. Device
passwords are not backed up, so devices reconnect with the one-line command or
`wiki drive connect` after Samba state is lost.

## Later clients

The drive's server model (current access per person, device registry, accepted
revision per wiki and the per-page edit publisher) does not depend on SMB. A
Windows Cloud Files client could use the same device registry, access rule and
edit pipeline over authenticated HTTPS instead of a file server.

## Writable inbox (optional)

With `WIKI_DRIVE_INBOX_SHARE` set, the same device login also maps that share as
**Wiki Inbox** (normally `I:`); without it, clients map only the drive. Each
device sees only its owner's folder, created when the person enrols a device or
calls `GET /api/me/inbox`. Accepted file intake is a separate service: this share
stores pending originals and never writes the wikis. Mount
`WIKI_DRIVE_DIR/inbox` read-write at `/shares/inbox` in Samba. The share has no
size limit of its own; use filesystem quotas or monitoring where that matters.
The application's UID must also be the intake service's UID so the default ACL
grants it access to files created by device accounts. Default ACLs grant only
that service UID and the person's group; no other device group may read or
write them.

`GET /api/me/inbox` returns the caller's folder name and currently contributable
wikis. A workload resolves to its registered owner's folder and must hold an
explicit contribute grant. Revoked agents and suspended people are refused.
The intake service must recheck grants before filing; a folder name grants no
wiki access. Device revocation and suspension use the existing account agent
and confirmation path for both shares. Pending changes remain pending.
