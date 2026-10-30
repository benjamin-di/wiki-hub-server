# Model worker

The application preserves a source, extracts its text and prepares an isolated
Git worktree before calling a worker. The worker receives no application, Git
or identity credential. The application validates and publishes the result.
An ordinary successful ingest saves automatically.

## Bundled worker

`wiki-runner` uses a fixed, operator-configured Chat Completions endpoint. It
provides only the current job's trusted root instructions, existing wiki pages
and new source reference to the model. The model returns bounded JSON file
edits. It receives no shell, Git, web, MCP or direct filesystem tool.

The worker validates every edit before writing any file. Only text content
under `wiki/` and `output/` can change; it rejects root files, originals,
references, symlinks, duplicate paths and traversal. The application then checks
preserved sources, file validity and the resulting Git tree before publishing.
Any worker, including an external one, that changes or adds a file under `raw/`
or `reference/` fails the job; only the application writes originals and
extracted references. A completed job's staged copy of its source is removed.

Required configuration:

| Setting | Meaning |
|---|---|
| `WIKI_MODEL_URL` | Complete fixed Chat Completions endpoint |
| `WIKI_MODEL_NAME` | Model available at that endpoint |
| `WIKI_MODEL_API_KEY` | Optional bearer credential; absent for an unauthenticated local model |
| `WIKI_MODEL_RESPONSE_FORMAT` | `json_schema` by default; `json_object` for compatible JSON-mode providers |
| `WIKI_MODEL_TOKEN_PARAMETER` | `max_completion_tokens` by default; `max_tokens` for compatible older endpoints |
| `WIKI_MODEL_OUTPUT_TOKENS` | Maximum output tokens, default 8192 |
| `WIKI_MODEL_CONTEXT_LIMIT` | Source/wiki context bytes, default 256000 |
| `WIKI_MODEL_RESPONSE_LIMIT` | Model response/edit bytes, default 2000000 |

Use a model that supports the selected parameters. There is no automatic paid
fallback, retry or API-account provisioning. Incomplete/refused responses and
provider failures leave a visible failed job for inspection. The worker requests
`store: false`; your provider's data-handling policy still applies.

The context budget reserves the complete new extracted reference and root
instructions, then selects whole wiki pages by lexical relevance within the
remaining budget. Oversized or lower-ranked pages are explicitly omitted. The
worker cannot replace an existing file whose complete content was not supplied.
An oversized source is rejected rather than truncated; explicit splitting or
conversion is required. Raising the budget alone does not establish scalability.
Qualify collection sizes and source-backed results as described in the
[roadmap](roadmap.md#c-useful-knowledge-and-reproducible-evidence).

Sources: [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [structured output modes](https://developers.openai.com/api/docs/guides/structured-outputs).

## External workers

You can keep an existing trusted sandbox instead. Set `WIKI_INGEST_URL` to its
private base URL. The application posts to `/run` with `run_id`, `vault` (the
worktree directory name), `prompt`, `timeout_seconds`, provider preferences and
log names. Shared working directories must use the same paths on both sides.

A successful response is:

```json
{"outcome": "success", "exit_code": 0}
```

Other outcomes do not publish. `/health` should include `active_runs` for backup
coordination. External agent runners must independently restrict filesystem
access to that one job's content directories and network access to their model
APIs. Do not run a general-purpose command executor with server credentials.

Qualify the configured worker's context selection and provider-attempt policy
separately; the bundled worker's limits and single model request do not describe
every external implementation. The Hub does not resubmit failed or uncertain jobs.

The application validates new evidence citations in the final merged tree.
Use Markdown links (percent-encode spaces or wrap the target in angle brackets),
backtick `raw/` or `reference/` paths, or plain paths without whitespace.
Source-derived prose needs evidence; navigation-only pages may link to other
pages. Pre-existing broken citations are tolerated during unrelated edits, while
new nonexistent targets prevent publication. Resolvable citations do not prove
that the model's claims are supported; evaluate factual quality separately.
