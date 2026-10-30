# Self-hosting

The example stack targets a Linux server with Docker Compose, Python 3.12+ and
uv. It uses Forgejo 17, Pocket ID, SQLite and the bundled HTTP-model worker.
All data uses new named volumes. Configure public HTTPS for three hostnames;
the application's model endpoint and credentials are your own choices.

## Prepare configuration

Clone a tagged release and run from its root:

```sh
uv run python examples/initialize.py
```

This creates `.env` with private permissions, generating missing deployment
credentials and preserving existing values. Keep that file outside Git and save
its encryption key independently of the server. Edit the three public URLs,
your Pocket ID username and email, the model name and its API key. Configure an endpoint
that supports Chat Completions and the selected JSON response mode; see
[the worker guide](ingest-runner.md). No model account is created by setup.

```sh
docker compose --env-file .env -f examples/compose.yml build app
docker compose --env-file .env -f examples/compose.yml up -d id git
```

The services listen on loopback. Leave the public proxy routes disabled until
the first administrator account has been prepared below. Pocket ID owns all
human sign-in; Wiki Hub never creates or handles a passkey.

The short-lived `storage-init` service sets the new volume roots to the runtime
users' IDs. It has no network or credentials and only the ownership capability.
The application, worker, Forgejo and identity service then run without root.

## Configure identity and Git

Create the internal Forgejo system account once:

```sh
docker compose --env-file .env -f examples/compose.yml exec git sh -eu -c '
  forgejo --config /etc/gitea/app.ini --work-path /var/lib/gitea admin user create \
    --username "$FORGEJO_USERNAME" --password "$FORGEJO_PASSWORD" \
    --email wiki-system@example.invalid --admin --must-change-password=false
'
```

An existing account is not replaced or rotated; verify its credential instead
of recreating it. Normal CLI users never receive this system password.

Prepare your first Pocket ID administrator and the scoped API and public clients:

```sh
docker compose --env-file .env -f examples/compose.yml run --rm identity-setup \
  --create-owner --enrolment-file /setup-output/enrollment.txt
```

The helper creates the named administrator only if absent, checks that public
first-account setup has closed, preserves matching clients and refuses
incompatible client settings. It enables only human-delegated `wiki:access`;
it creates no machine grant. With the generated web secret, it also registers a
separate confidential `wiki-web` client with PKCE, an exact browser callback,
launch URL and logo. Its configured secret is used only by the server. Put the returned `owner_subject`
in `.env` as `WIKI_OWNER_SUBJECT`; this is an identifier, not a credential.

Enable your HTTPS proxy routes; [the Caddy example](../examples/Caddyfile) shows
the three routes. Replace its example hostnames. Open the link in the private
`.setup/enrollment.txt` file in your own browser and register your passkey.
The link expires after one hour. Delete that file after enrollment and keep it
out of logs and support messages. On later configuration runs, omit both
`--create-owner` and `--enrolment-file` to avoid issuing another login link.
Then start the application and worker:

```sh
docker compose --env-file .env -f examples/compose.yml up -d
curl --fail https://wiki.example.com/health
uv tool install .
wiki --server https://wiki.example.com login
wiki create starter --title "Starter wiki"
```

Open `WIKI_PUBLIC_URL` in a browser, or launch Wiki Hub from Pocket ID's **My
Apps**. To search people before their first Wiki Hub login, supply a dedicated
read-only directory key as described in [web setup](web-ui.md). Existing `.env`
files gain web secrets when `examples/initialize.py` is run again; retain those
values when repeating identity setup. Never replace the directory key with the
bootstrap static administrator key.

`WIKI_HUB_IMAGE` can select the matching published image instead of a local build.
Pin a release version or digest deliberately when operating a server. Back up
the `app`, `workspaces`, `git-data`, `git-config` and `identity` volumes together
with the deployment configuration before upgrades. See [operations](operations.md).

## Existing services and other identity providers

You can run only the app and worker against an existing private Forgejo 17.
Set `FORGEJO_URL`, `FORGEJO_PUBLIC_URL`, `FORGEJO_USERNAME` and
`FORGEJO_PASSWORD` to that dedicated backend. Disable user-editable Git hooks,
registration, actions and migrations, and keep managed repositories private.

An alternative identity provider must issue RS256 RFC 9068 access JWTs with
`typ: at+jwt`, `sub`, `iat`, `exp`, the exact `/mcp` resource audience and
`wiki:access` scope. It must expose JWKS and support the device flow and refresh
tokens for the CLI. Configure the exact issuer without changing its identifier.
Opaque access tokens and general-purpose login ID tokens are not supported.
Register hosted clients using [their exact callbacks](clients.md); the example
Pocket ID API helper is specific to Pocket ID.

Reference installations:
[Forgejo 17](https://forgejo.org/docs/v17.0/admin/installation/docker/) and
[Pocket ID](https://pocket-id.org/docs/setup/installation).
