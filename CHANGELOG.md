# Changelog

All notable changes are recorded here. This project follows Semantic Versioning.

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
