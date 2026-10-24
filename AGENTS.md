# Wiki Hub development

The application code, tests, examples and documentation are published. Keep
deployment configuration, people, private domains, credentials and real wiki
material outside this repository. Use synthetic sources and reserved example
domains in fixtures. Never save a live response as a test fixture.

Run `make check`. Tests use disposable Git/LFS repositories and fake identity,
Forgejo and model endpoints. They must not use a real account or model API.
Keep the Python 3.12 server and the Windows credential path working.

The control plane decides access; Forgejo stores accepted Git/LFS content.
Check current grants for API and MCP operations, including sessions already
connected. Agents have explicit grants capped by their owner's access. Copy
policies must revoke existing managed Git credentials before reporting success.
Do not treat pending backend changes as completed permission changes.

Ingests preserve original sources and faithful extracted references. Model
workers cannot change root instructions, run privileged Git operations or read
other wikis. Ordinary accepted ingests commit automatically. Failed/uncertain
jobs remain visible without an automatic model retry. Never force a Git merge
or publish unresolved conflicts. Rehearse schema changes on copied state.

Public releases are clean exports from the private source. The publication
configuration and private workflows are excluded from the export. Public
workflows must run independently using public dependencies and GITHUB_TOKEN.
Do not add private deployment paths or dependencies to make the public build pass.
