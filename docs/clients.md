# Clients

## CLI and local MCP

Install the CLI from the selected release checkout with `uv tool install .`.
Run `wiki --server https://wiki.example.com login`, approve the fresh device
code with your passkey and keep the terminal open until it reports signed in.
Subsequent commands use that sign-in and refresh it when needed.

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
