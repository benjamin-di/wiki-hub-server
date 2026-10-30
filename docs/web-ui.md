# Web interface

Wiki Hub serves its browser interface at its existing public URL. Sign in, open
**My wikis**, then choose **Share** to add a person, change a role or remove access.
The same current grants authorize browser, API and MCP operations. A copied wiki
link confers no access. Only wiki owners and delegated admins can list members
or change grants; instance-wide account controls remain separate.

The interface provides wiki discovery and creation, sharing, pending-change
recovery, grant activity, copy/archive settings, agent registration and revocation,
drive-device management and instance-owner account administration. All controls
reuse the service's authorization and backend confirmation rules.

## Implementation status and remaining work

Browser sign-in uses a dedicated OIDC client, encrypted sessions and signed
provider back-channel logout. Wiki managers can refresh existing members' names
from the directory without altering grants. Agent credentials and drive passwords
appear once and are cleared when dismissed; save them securely first. Only the
configured instance owner can register agents or administer account suspension
and per-person copy policy. Delegated wiki managers cannot use those global controls.

Source checks cover grant changes, pending recovery, session revocation and
administrative authorization. See [audit findings](audit.md#browser-and-sharing-audit).
Installation acceptance still requires:

1. Release and deploy the independently reviewed source. Browser routes precede
   the MCP mount and preserve any separately configured gateway transport.
2. Configure the dedicated browser client, session key and read-only directory
   adapter privately. Rehearse the additive schema upgrade on copied state.
3. Verify Pocket ID My Apps discovery, real sign-in/refresh/logout, recipient
   selection before first login, delegated sharing, backend/directory outages,
   and revocation in an existing browser session.
4. Have someone unfamiliar with the CLI complete add/change/remove access. Check
   keyboard, screen-reader and mobile operation. Automated browser checks do not
   establish WCAG 2.2 AA or the usability target.

Invitations, group grants, expiring access, access requests, notifications,
ownership transfer and content editing remain deliberate product deferrals.

## Enable browser sign-in

The example initializer and identity helper configure the web client:

```sh
uv run python examples/initialize.py
docker compose --env-file .env -f examples/compose.yml run --rm identity-setup
```

Keep `.env` private and back it up with application state. The initializer
preserves existing values and generates `WIKI_WEB_CLIENT_SECRET` and
`WIKI_WEB_SESSION_KEY` if missing. The helper registers a confidential `wiki-web`
client with PKCE, a callback at `WIKI_PUBLIC_URL/auth/callback`, the existing
`wiki:access` delegated resource permission, and a logo/launch URL for Pocket ID's
**My Apps**. No machine permission is added and no secret is printed.

The helper preserves an existing matching client and its secret. Changing the
secret in `.env` alone does not rotate it in Pocket ID. For a deliberate rotation,
set a new client secret in Pocket ID and the matching private configuration
together. If initial identity setup was interrupted after client creation but
before secret registration, repair that client's secret in Pocket ID before
continuing. Incompatible existing callbacks or launch URLs require operator
review; the helper does not silently rewrite them.

For another supported identity provider, configure these server settings:

| Setting | Purpose |
| --- | --- |
| `WIKI_WEB_CLIENT_ID` | A dedicated browser OIDC client, separate from CLI/MCP consumers. |
| `WIKI_WEB_CLIENT_SECRET` | Its confidential-client secret. Public PKCE clients can omit it. |
| `WIKI_WEB_SESSION_KEY` | At least 32 random characters for encrypting browser login/session records. |

Register an exact callback such as `https://wiki.example.com/auth/callback`,
enable authorization code with S256 PKCE, and permit `openid profile email
offline_access wiki:access` for the existing wiki resource audience. The server
uses client-secret Basic authentication for a confidential web client. It verifies
ID tokens against the configured issuer's keys and browser client ID; API
authorization still requires the existing scoped access-token contract. The
browser interface requires HTTPS, with an exception for loopback development.
Its session key and client secret must never be distributed to browsers.

Without a configured web client and session key, the root page displays setup
guidance. Existing CLI/API/MCP clients continue to work. The web app does not
change their scopes, audiences or authentication method.

## Enable the Pocket ID people directory

Pocket ID's current API-key creation does not support restricting a key to
`users:read` and `oidc-clients:read`. Do not give Wiki Hub a provider administrator
key. Run an identity-owned read-only adapter with an independent application
credential, or use a provider that offers equivalent narrowly scoped reads.
Configure `WIKI_DIRECTORY_URL` with the adapter's trusted base URL and
`WIKI_DIRECTORY_API_KEY` with its independent credential. Leave both empty to use
verified local profiles instead. The Compose example does not provision an adapter.

The compatible adapter implements only `GET /api/users?search=…`,
`GET /api/users/{subject}` and `GET /api/oidc/clients/{web-client-id}`. It returns
minimal profile fields (`id`, `displayName`, `username`, `email`, `disabled`,
`userGroups` containing IDs only), and client admission fields
(`isGroupRestricted`, `allowedUserGroups` containing IDs only). Search results
use a `data` array. Authentication uses `X-API-Key`. Bound searches and results,
filter disabled and nonadmitted people, and reject other routes and writes.
Provider administration credentials stay inside identity infrastructure.

The picker works before a recipient's first Wiki Hub login. Searches are bounded
and rate limited and require current management access to the target wiki.
Results expose only immutable subject, name, distinguishing email and username.
Disabled accounts, people outside the web client's admission groups, and locally
suspended accounts are excluded. The server resolves the selected identity again
when granting access. Identity names and email addresses are display values, not
grant keys; changing an email cannot transfer access to a different subject.

With no directory configured, search uses profiles from previous verified web
sign-ins. An advanced exact-subject entry supports other issuers and accounts not
yet seen. When a configured directory is unavailable, membership and removal still
work; new grants wait for directory verification. Unknown members retain an
administrative identity fallback so they can always be removed.

## Roles and completion states

| Web label | Stored role | Capabilities |
| --- | --- | --- |
| Viewer | `read` | Read pages, sources and history. |
| Contributor | `contribute` | Also submit sources; accepted ingests save automatically. |
| Editor | `write` | Also edit through Git/drive and restore history. |
| Manager | `admin` | Also manage wiki access and settings. |

The owner's row is fixed. Self-demotion requires confirmation. Agents retain
their existing Viewer/Contributor ceiling, explicit grants and owner-access cap.
The interface shows when current account, agent-owner or archive restrictions
reduce a stored grant's effective access. Archived content remains read only.

A backend outage does not produce a successful permission change. The affected
grant remains pending and Wiki API access is blocked; Git or drive access may
retain its previous state until the update finishes. The row stays visible across
reloads and server restarts. **Retry update** explicitly retries the current
operation after checking current authority and the access snapshot. Refreshing or
reading status does not retry mutations.

Grant changes through the service, including CLI/API changes, have durable
receipts and record actor, target, previous/requested role, timestamps and outcome.
Recent activity is visible only to wiki managers. Existing grants are preserved
without inventing historical audit events. Newer changes supersede older pending
operations; retrying an old operation cannot overwrite newer intent. Stale browser
writes are rejected for review. A lost reply can be checked through the operation
receipt and current membership without issuing the mutation again.

This activity journal covers **grant changes**. It is not an audit of content,
copy-policy changes, account suspensions or provider administration, and it is not
a tamper-proof external log. Managed copy policy controls Git/drive eligibility;
it cannot recall downloaded content or prevent all copying of readable sources.

## Session and deployment boundaries

Browser cookies hold random opaque identifiers with Secure, HttpOnly and SameSite
protection. Login transactions are bound to the initiating browser and consumed
once. Access/refresh tokens and CSRF secrets are encrypted in SQLite, not exposed
to JavaScript, browser storage or URLs. Sessions expire after an hour idle or
twelve hours absolute. Tokens are verified and local active-account/current-wiki
checks run on protected requests, including after token refresh.

Cookie-authenticated operations live under `/ui/api/`, with Origin and CSRF checks
for mutations. Existing `/api/` and `/mcp` authentication remains unchanged; browser
cookies cannot authorize those routes. Private pages/responses are `no-store`,
have a restrictive CSP, and use locally packaged scripts, styles and logo.
Sign out discards the local browser session. It does not sign out other apps.

Register `WIKI_PUBLIC_URL/auth/backchannel-logout` as the browser client's
back-channel logout URL when the provider supports it. Signed RS256 logout tokens
must match the issuer/client, contain the logout event and identify a subject or
provider session. Freshness, replay protection and revocation watermarks prevent
old notifications or concurrent login completion from restoring revoked sessions.
Provider delivery failures return a retryable 503. Local Sign out discards only
this browser's Wiki Hub session.

Provider disablement or consent revocation is immediate only if the provider
sends a valid logout notification; otherwise an issued access token may remain
valid until expiry. For immediate local offboarding, use **Administration** or
the existing suspension command. Account/owned-agent blocking is immediate;
managed credential revocation must finish before the update reports success.
Current per-wiki grants apply independently to already connected sessions.
