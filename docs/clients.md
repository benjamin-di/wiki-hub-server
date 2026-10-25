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
