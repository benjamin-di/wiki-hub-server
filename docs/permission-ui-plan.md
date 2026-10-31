# Browser access management design

The browser sign-in, sharing and administration plan proposed on 30 October 2026
is implemented in source. This note retains the design decisions and deliberate
deferrals. Use [web setup and supported behavior](web-ui.md) for configuration,
current controls and the installation acceptance checklist, and the
[roadmap](roadmap.md#outstanding-work-after-the-source-audit) to distinguish
implementation from delivery and qualification.

## Product boundary

People open Wiki Hub at its existing origin, sign in through the configured
identity provider, find a named wiki and choose **Share**. A dedicated browser
client can appear in the provider's app launcher; direct bookmarks work too.
A wiki link is navigation, never a grant. Admission to the application grants no
wiki access.

The browser calls the same control plane as CLI, API and MCP. The identity
provider owns accounts and passkeys; Wiki Hub owns wiki grants, registered
services, copy settings and ingestion. Provider administration credentials stay
outside the application.

The implemented screens are:

- **My wikis**: authorized discovery, role and archive state; creation only for
  the configured instance owner.
- **Wiki access / Share**: named recipient selection, individual role changes,
  removal, pending recovery and grant activity; membership is manager-only.
- **Wiki settings**: managed-copy policy and archive/reopen controls.
- **Wiki access** at `/agents`: registered services with effective wiki roles,
  names/purpose and usage observations; observed OAuth apps are shown separately.
- **Connections**: drive setup and device revocation.
- **Administration**: instance-owner account suspension and user copy policy.

## Interaction principles

Show requested and effective access separately. A saved intent is not a
confirmed backend change. Explain Viewer, Contributor, Editor and Manager beside
the role control; use Viewer as the default. Ownership remains fixed. A manager
can administer only their wikis; global account and registration controls retain
the existing instance-owner restrictions.

Sharing uses an authorized, bounded people picker. Resolve the selected immutable
subject again on submission. Names and email addresses are display data, not
authorization keys. A read-only provider adapter permits selection before the
recipient's first login. Directory failure preserves cached membership and
removal; it cannot silently approve an unverified new recipient.

Grant changes use conditional access versions and durable operation receipts.
Lost replies are resolved by observing the receipt and current membership,
without replaying over newer intent. Confirmation waits for the current snapshot;
a failed refresh leaves a reload notice. Pending projection stays visible and
explicit retries recheck current authority. Grant activity records intent and
outcome for managers; it is not a general administration or tamper-proof log.

Other settings reuse their existing service contracts. After a lost reply,
refresh their state before retrying. Do not extend grant receipts and concurrency
versions to every control without a demonstrated need. A lost one-time credential
requires review and revocation of the unusable registration before replacement.
Never claim that downloaded material has been recalled by denying managed copies.

## Security and implementation decisions

Use the existing Starlette application, packaged templates, local styles and
small JavaScript assets. No separate frontend service or policy engine is needed.
The browser uses OIDC authorization code with PKCE, expiring single-use login
transactions and encrypted server-side sessions. Cookies contain only opaque
identifiers. Browser mutations require CSRF and Origin checks; bearer API and
MCP routes do not accept browser cookies.

Current grants, account status and agent-owner ceilings apply to protected
requests, including existing sessions and refreshed tokens. On authority loss,
clear protected views. Provider logout hooks supplement local enforcement;
provider admission and token expiry are separate from immediate local suspension.
Backend paths, credentials and private diagnostics must not reach UI errors.

Schema changes are additive and rehearsed on copied state. Preserve ownership,
grants, pending changes and issuer/subject identity. Automated checks use only
synthetic identity, directory, Git and file-server endpoints. Keep real runtime
acceptance and private records outside public fixtures and documentation.

## Acceptance and deliberate deferrals

The [web checklist](web-ui.md#implementation-status-and-remaining-work) covers
installed identity, real sharing/revocation, usability and accessibility. Source
regressions cover outages, restarts, stale tabs, duplicate clicks, lost replies,
concurrent administrators and authority loss; they do not establish a person's
first login or physical device behavior. Initial usability target: add, change
or remove access within 30 seconds after opening the wiki, excluding sign-in
and backend outages. Validate keyboard, screen-reader and mobile interaction;
automated Chromium cases alone do not establish WCAG 2.2 AA.

Invitations, group grants, expiry, access requests, notifications and ownership
transfer remain demand-led product decisions. Browser content editing is a
separate scope. Public sharing links, a generic permission-matrix builder and
a new policy engine are outside this design.
