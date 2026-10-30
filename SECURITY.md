# Security

Report vulnerabilities privately through [GitHub's vulnerability reporting](https://github.com/benjamin-di/wiki-hub-server/security/advisories/new).
Please omit credentials, real sources and private wiki content from reports.
Only the latest tagged release is supported.

Authentication requires audience-bound, scoped access tokens. Wiki permissions
live in the application database; ordinary readers receive no additional wiki
access. Agent credentials are separate, revocable and explicitly scoped. The
application's privileged Forgejo credential stays on the server.

The optional `/gateway/mcp` transport instead requires a dedicated backend
credential and a gateway-asserted immutable human subject. Enabling it trusts
that gateway to authenticate humans and overwrite identity headers. It shares
the normal per-wiki authorization checks and accepts no machine subjects.
Its credential cannot authorize the REST API or direct OAuth MCP transport.

Keep the model worker on a private application network. The bundled HTTP-model
worker accepts bounded text edits and executes no model tools or commands. It
has no Git or identity credential. External workers must enforce the equivalent
one-wiki filesystem and model-only network boundaries themselves.

Wiki administrators and the service operator are trusted. Operators can access
the stored content and deployment credentials. Read access lets a recipient save
what they receive; copy controls cannot recall downloaded files or already-issued
download links. Local Git copies and backup retention need their own operational
policies. Do not store production knowledge until a populated off-site restore
has been verified.

Sources are evidence of what they say. Automatic ingestion does not independently
verify facts or make a source trustworthy. Root instructions stay outside model
edits; history and rollback support inspecting and reversing an accepted update.
