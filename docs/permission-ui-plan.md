# Wiki Hub web interface and access management plan

Proposed 30 October 2026. The initial browser sign-in and grant-management release
is now implemented in source. See [web setup and supported scope](web-ui.md) for
configuration, validation and the remaining features described below. Installation
acceptance and deployment are separate. All example people and collections are synthetic.

## Current delivery status

The [implementation status](web-ui.md#implementation-status-and-remaining-work)
is the authoritative completion and remaining-work checklist. Browser entry,
individual-account sharing, pending recovery and grant activity are implemented.
Copy/archive settings, agent and device screens, owner account administration,
provider logout hooks and explicit member-label refresh are implemented. The
screens below retain the design intent; installation and usability acceptance
remain separate. The permission journal records grants rather than all policy
or account administration.

## Recommendation

Give Wiki Hub a small, polished web application at its existing public origin.
Make it discoverable as **Wiki Hub** in Pocket ID's **My Apps** dashboard. Pocket
ID provides account identity and passkey sign-in; Wiki Hub explains and manages
wiki access. Keep the CLI, API and MCP as equivalent clients of the same control
plane.

Use the familiar sharing interaction from Google Drive or Notion: open a wiki,
choose **Share**, find a person, choose a clearly explained role and apply it.
Use the clear navigation and contextual settings associated with Linear, and
the attributable access history associated with GitHub. These are design
references, not claims of feature parity or a comparative product evaluation.

The first release should let someone grant, change and remove access without
installing a CLI, exchanging subject IDs or opening an identity admin console.

## First principles

- Start with the object people understand: a named wiki and the people using it.
- Show both the requested permission and what is effective now. A saved request
  is not necessarily a completed change.
- Explain capabilities where people choose them. A role name alone is not enough.
- Make common actions short; explain consequences before elevated or destructive
  changes. Avoid confirmations for every ordinary action.
- Use one authorization implementation across every client. The browser must not
  become a second permission system.
- Keep the initial product small enough to operate with the existing Python
  service, SQLite and Forgejo.

## How users arrive

1. Open Pocket ID, choose **My Apps → Wiki Hub**.
2. Wiki Hub starts an OIDC redirect if its own session is absent. An existing
   Pocket ID session can make this brief; consent and reauthentication still
   follow the identity provider's policy.
3. Land on **My wikis**, or return to the authorized wiki URL originally opened.
4. Open a wiki and choose **Share** if the user can manage it.

Direct bookmarks work too. Add a **Manage access on the web** link to CLI help
and relevant drive documentation. A copied wiki URL is a navigation link: it
does not confer access. A denied deep link shows a neutral access message without
revealing a private wiki's title or membership.

Pocket ID already supports `launchURL`, logos and group-filtered app discovery.
This was checked in upstream **v2.19.1**, the version pinned in the example stack,
as well as current documentation. Register one dedicated `wiki-web` OIDC client,
with a launch URL such as `https://wiki.example.com/`, exact callback
`https://wiki.example.com/auth/callback`, and an app logo. Keep the other CLI and
hosted-consumer clients separate. Do not create an identity client for every wiki.
Configure admission deliberately; a tile or provider group grants no wiki access.

No Pocket ID fork or embedded administration screen is needed. Keep this a
normal application launch and sign-in flow, usable with other supported issuers.

## Screens and information hierarchy

**My wikis** is the default page. Show title, short overview, the caller's role,
and archived state. Search and filter the caller's accessible wikis. Put **Share**
on manageable wiki rows and on their overview page. Only the configured instance
owner sees **Create wiki**, matching current server policy. A new user with no
wikis sees a useful explanation and a way to copy their identity details for
support; do not expose a global catalog of private collections.

**Wiki overview** provides context, the caller's effective capabilities and links
to supported ways to use the wiki. Its **Access** page is the canonical deep link
for the Share panel. Only wiki owners and delegated admins can list membership
or change access. Readers see **Your access**, without the membership directory.
Do not turn this first release into a full document editor.

**Share / Access** is a focused dialog on desktop and a full-width screen on
mobile. It contains:

- The wiki title and a concise explanation that access is explicitly granted.
- A searchable **Add a person** picker, with name and a distinguishing username
  or email. Pick a concrete result, then choose a role; typing an email is not
  itself an invitation or permission change.
- Existing members, including the immutable owner, with role and status on each
  row. Existing grants with unresolved identities remain visible and removable.
- Role changes applied per person. The row has an explicit **Save** action when
  edited; no ambiguous dialog-wide **Done** action that also commits changes.
- A clearly labelled **Remove access** action with a short named confirmation.
- A **Copy wiki link** action that explains the link only works for people who
  already have access, and a link to access activity.

Search defaults to people. Add a separate **Agents** view when agent management
ships, with owner attribution, role ceilings and effective-access explanations.
Do not mix opaque workload subjects into the human picker.

**Settings and connections**, added after the permission workflow, provide wiki
copy policy, archive controls, drive setup and device revocation. Global user
suspension and user-level copy policy belong under instance-owner-only
**Administration**, distinct from a wiki's settings. Retain existing restrictions
on wiki creation, agent registration and ownership.

## Role language

Keep the existing server role values and introduce friendly presentation labels.

| UI label | API role | Explanation beside the choice |
| --- | --- | --- |
| Viewer | `read` | Read pages, sources and history. |
| Contributor | `contribute` | Also submit sources; accepted ingests save automatically. |
| Editor | `write` | Also edit through supported Git/drive workflows and restore content history. |
| Manager | `admin` | Also manage access and wiki settings. |
| Owner | Ownership | Full wiki access; fixed by the current ownership model. |

Show **Viewer** as the default for new grants. Make the selected role's full
description visible. A Manager promotion explicitly says it allows sharing with
other people. Confirm self-demotion because it can remove access-management
capability. The owner row is locked, with an explanation; do not imply an
ownership-transfer feature exists.

Separate stored role from active capabilities. Archived wikis retain their grants
but block content changes. Agents only support Viewer/Contributor, require an
explicit wiki grant and are capped by their owner's current access. A revoked
agent or suspended owner cannot be fixed by increasing the displayed grant.

Do not label copy policy **Prevent copying**. Its precise scope is **Allow managed
Git copies and network drive access**; user and wiki policy both apply. It does
not recall downloaded material or prevent every way of copying readable content.

## Complete interaction and failure states

Adding someone: select person → select role → **Add** → applying state → confirmed
member row. Editing: select new role → **Save** → same process. Removing: **Remove
access** → confirm the named person and wiki → persistent removal status until
the server confirms completion. An optional email notification is later work;
do not claim that an email was sent.

The UI must model these states explicitly:

| State | What the interface says and does |
| --- | --- |
| Confirmed | Show the effective role. Success follows backend confirmation. |
| Applying | Disable duplicate submission for that row and show progress text. |
| Backend pending | Keep the row and desired role/removal visible; show a persistent warning and explicit retry. |
| Reply lost / outcome unknown | Refetch the operation and current access; retain uncertainty until observed. |
| Rejected before change | Preserve the edit, show the specific safe explanation, allow correction. |
| Concurrent change | Reload current access and ask the user to review their edit again. |
| Caller lost permission | Clear protected data and editing controls when the next authorized request fails. |

For a pending removal, say: **Wiki API access is blocked. Git or drive access may
remain until the update completes. Retry to finish removing access.** Adjust the
text to the actual known status; do not claim a backend was revoked if it is
unavailable. Pending copy-policy and suspension changes need equivalent honesty.

Refresh status on page focus and use bounded polling while an operation is in
progress. Reading status must not silently resubmit mutations. After an error or
restart, pending work must remain discoverable. Never automatically undo a
revocation or rerun a model job to make the interface look successful.

## Identity and a useful people picker

The current state stores subjects, not a human directory. A frontend alone cannot
produce a reliable recipient picker. Add a provider-neutral display-profile
record keyed by configured issuer and immutable subject, populated from verified
sign-in/userinfo. Names and emails are display data, never authorization keys.

For Pocket ID, add a server-side read-only directory adapter so a person can be
selected before their first Wiki Hub login. Use an identity-owned adapter with an independent credential. Pocket ID API
keys cannot currently be narrowed to directory-read scopes; never lend the
provider administrator key to the running application. Filter the exposed search to the configured Wiki Hub admission
population, exclude disabled or locally suspended recipients for new grants, and
return only minimal identity fields. Do not expose Pocket ID's raw user records,
groups or administrative endpoints to a browser. Provider admission policy and
Wiki Hub suspension remain distinct; this work must not suggest provider account
disablement instantly revokes existing Wiki Hub tokens.

Authorize directory lookup for an active caller who currently manages the target
wiki. Bound and rate-limit searches and results; re-resolve a selected immutable
identity on submission. Do not expose an unrestricted public directory. Fetch
minimal profiles for existing members independently of search availability.

Directory failure must not hide membership or block removal. Show cached labels
as such, with safe fallback identity details for administrators. Other issuers
can use verified profiles from prior sign-ins; an advanced exact-subject flow is
a compatibility fallback. A usable pre-login directory is part of the Pocket ID
first-release acceptance, not a cosmetic enhancement to defer.

## Architecture and missing contracts

Serve server-rendered templates, local CSS and small JavaScript modules from the
existing Starlette application. Declare template dependencies directly. Use
accessible native controls and a tested dialog/focus implementation. This workflow
does not require another deployed service, a separate SPA or a document-editor
framework. Ship the assets in both the Python wheel and container/public export.

Use authorization code with PKCE through a maintained OIDC client library, with
state, nonce, exact redirects and verified issuer/audience/signature/expiry. The
web client is confidential if the provider supports that mode; its secret stays
server-side, with PKCE enabled too. Request the existing `wiki:access` resource
permission alongside the identity scopes needed for display. Do not use ID tokens
to authorize API operations.

Keep access/refresh tokens encrypted in server-side session storage, keyed by a
random opaque `Secure`, `HttpOnly`, `SameSite=Lax`, host-only cookie. Use expiring,
single-use login transactions and session rotation. Use idle and absolute session
limits and explicit logout. Implement provider logout/session-revocation hooks
where available; local suspension and current grants must independently block
active browser sessions. Validate server-held access tokens and current local
authorization on protected requests, including after refresh.

Add cookie-authenticated browser routes under `/ui/api/` that call the same
service methods as REST/MCP. Keep existing `/api/` routes bearer-only. Protect
browser mutations with CSRF tokens and Origin checks; use POST for logout and
other changes. Use a restrictive content security policy, output escaping,
`no-store` on private responses, same-origin assets and local-only return URLs.
Do not store tokens in browser storage or URL fragments. No offline cache of
protected pages. Clear sensitive in-memory views on expiry or permission loss.

Existing service/API operations already cover wiki listing, effective access,
sharing/removal, copy settings, archival, agents and devices. Extend them with:

- Browser sign-in, callback, logout and server-side session lifecycle.
- Minimal display profiles and authorized recipient search.
- A cheap wiki/access summary, avoiding Git content fetches for every access-page
  refresh; explicit current capabilities and server-owned state descriptions.
- A versioned access snapshot and conditional browser writes to detect concurrent
  edits. Preserve existing clients; route every writer through the version update.
- A durable mutation receipt/operation identifier, so a lost reply can be queried
  without replaying over a later administrator's change. Retry only the still
  current intended change and recheck authority. Distinguish backend failure
  after saving intent from rejection before mutation. Existing `pending` fields
  remain authoritative; this is not the ingestion job API.
- Access activity recording actor, target, before/after role or policy, timestamps,
  operation ID and pending/completed/failed outcome. Record intent with the state
  change and completion after projection; activity is admin-only and contains no
  credentials. CLI/API changes must appear too. This is an access audit, not a
  general event bus or a claim of tamper-proof external logging.

Use additive SQLite migrations, rehearsed on copied state. Preserve exact issuer
identity, current subjects, ownership, existing grants and durable pending states.
UI errors must not reveal backend paths, credentials or private diagnostics.

## Delivery sequence

1. **Browser entry and sign-in.** Add the packaged shell, sessions and OIDC web
   client. Extend the example identity helper to register/validate the client,
   launch URL and logo without rewriting incompatible existing clients. Deliver
   My wikis, neutral denied/empty states and authorized deep-link return.
2. **Complete access workflow: first useful release.** Add profile/directory
   lookup, sharing dialog/page, role changes/removal, truthful pending states,
   conditional writes, durable receipts and access activity. Include an
   end-to-end Pocket ID → Wiki Hub acceptance rehearsal. Release these two slices
   together to ordinary users; a login page alone does not solve the problem.
3. **Related controls.** Add copy/archival settings, agents and connection/device
   screens, and instance-owner user administration. Explain effective access and
   restrictions throughout. Reuse existing authorization boundaries.
4. **Evidence-led additions.** Consider group grants, expiry, invitations to people
   without accounts, access requests, notifications and ownership transfer only
   after observing use. Each requires backend policy/lifecycle work. Content
   editing is a separate product decision.

Avoid public sharing links, a generic permission-matrix builder and a new policy
engine in the initial release. No live account, deployment change or provider
configuration is required to review this plan and its synthetic prototype.

## Acceptance and validation

- An owner or delegated wiki admin can add a known Pocket ID user, change their
  role and remove them through the browser without subject-ID exchange. Initial
  usability target: each task within 30 seconds after opening the wiki, excluding
  sign-in and backend outages; validate with someone unfamiliar with the CLI.
- A read-only user cannot see membership, enumerate recipients or change access.
  A wiki admin gains no authority over other wikis or global account settings.
- Duplicate names, renamed emails, disabled users, directory outages, unknown
  existing subjects and users who have never signed in behave as specified.
- Existing browser sessions lose authorization after revocation/suspension; agents
  remain constrained by current explicit grants and their owner's access.
- A Forgejo/drive failure, process restart, stale tab, duplicate click, lost reply
  or concurrent administrator never produces a false success. Explicit retries
  finish the current intended operation without overwriting newer intent.
- No managed-credential revocation or archive/copy change is declared complete
  before its existing backend contract is satisfied. Prior downloaded copies are
  not claimed to have been erased.
- Test login transactions, code replay, session expiry/refresh/logout, CSRF, XSS,
  open redirects and browser-cookie/API separation with fake identity endpoints.
- Verify keyboard-only operation, focus/escape/return behavior, labelled role
  descriptions, screen-reader status announcements, contrast, reduced motion and
  layouts at 360px and desktop widths. Target WCAG 2.2 AA, including touch targets.
- Run `make check`, add browser integration tests with synthetic data and fake
  backends, and retain wheel/container asset smoke checks. Keep Python 3.12 and
  Windows credentials/drive workflows working. Run schema rehearsals on copied
  synthetic state; perform any later real installation acceptance privately.

## Sources and implementation anchors

- [Pocket ID app dashboard documentation](https://pocket-id.org/docs/configuration/appdashboard)
- [Pinned Pocket ID launch URL DTO](https://github.com/pocket-id/pocket-id/blob/v2.19.1/backend/internal/dto/oidc_dto.go)
- [Pinned My Apps UI](https://github.com/pocket-id/pocket-id/blob/v2.19.1/frontend/src/routes/settings/apps/+page.svelte)
- [Pinned directory authorization](https://github.com/pocket-id/pocket-id/blob/v2.19.1/backend/internal/controller/user_controller.go)
- [Current server routes](../src/wiki_hub/server.py),
  [authorization and sharing](../src/wiki_hub/service.py),
  [control-plane state](../src/wiki_hub/state.py),
  [identity setup helper](../examples/configure-identity.py),
  [sharing tests](../tests/test_sharing.py).
