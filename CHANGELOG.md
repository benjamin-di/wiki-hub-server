# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

## [0.18.2] - 2026-10-31

### Fixed

- Refresh source activity after an unconfirmed submission reply, including when an older activity request is still running. Keep the original receipt and draft until the submission is confirmed (78a3aa4).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.18.1; no catalog schema changes.

## [0.18.1] - 2026-10-31

_No changes to the published package; correct standalone export qualification fixtures._

### Upgrade

- Action required: none.
- Rollback: compatible with 0.18.0.

## [0.18.0] - 2026-10-31

_Yanked: public export qualification rejects a synthetic fixture URL; use 0.18.1._

### Added

- Create a clean wiki from its title and optional purpose, with a generated short name under Advanced. Open its overview with a private-access explanation and separate sharing action (fe3fd37).
- Add a file or pasted text from the overview using the existing source-preserving pipeline. Show recent job outcomes and recover a lost submission reply without starting another ingest, preserving edited drafts and the submission identity through denied retries (8de3c7a).
- Keep the fictional example in optional overview Help instead of inserting example records into new collections. Existing collections are preserved (fe3fd37).

### Upgrade

- Action required: none. Existing example collections are not changed automatically.
- Rollback: compatible with 0.17.0; no catalog schema changes. New seed content and ingests remain ordinary Git history.

## [0.17.0] - 2026-10-31

### Added

- Replace the generic Agents list with an access overview showing each service's effective wiki roles, editable name and purpose, credential type and last observed use. Retain revoked registrations in collapsed history.
- Show observed apps signed in as the current person, with identity-provider authorization guidance. Record authenticated client IDs in new ingest jobs and accepted commits.
- Accept an originating client assertion from an authenticated trusted gateway; public caller headers cannot set attribution.

- Usage observations start after upgrade; earlier jobs retain unknown client attribution. Existing grants, workload bindings and revocations are preserved.

### Upgrade

- Action required: optionally set `WIKI_AUTHORIZED_APPS_URL` to the provider's app authorization page. Gateways should overwrite `X-Wiki-Client` from the verified client claim alongside `X-Wiki-Subject`.
- Rollback: compatible with 0.16.3. Added catalog columns and the observation table may remain; older versions ignore them.

## [0.16.3] - 2026-10-31

_Releases 0.1.0–0.16.2 were not recorded here; see their tags._

### Fixed

- Keep browser and CLI sign-in available by rejecting their configured client IDs when registering workload agents (211ab4b).
- Recheck current wiki access when restoring the Share page through browser history (211ab4b).
- Preserve browser sessions during temporary identity-provider refresh failures so sign-in can be retried (999d061).
- Refresh known account names in Administration without replacing unsaved controls, and retain cached names during directory outages (bc8002d).
- Guide users to review and revoke agent or device credentials before replacing them when a registration reply is lost (784204b).
- Confirm sharing changes against current membership before showing success, and explain pending actions or unavailable status explicitly (784204b).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.16.2.

[0.16.3]: https://github.com/benjamin-di/wiki-hub-server/releases/tag/v0.16.3

[0.17.0]: https://github.com/benjamin-di/wiki-hub-server/releases/tag/v0.17.0

[0.18.0]: https://github.com/benjamin-di/wiki-hub-server/releases/tag/v0.18.0

[0.18.1]: https://github.com/benjamin-di/wiki-hub-server/releases/tag/v0.18.1
