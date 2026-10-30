"""One tool catalog for remote OAuth clients and the signed-in stdio CLI."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from .auth import principal_subject
from .forgejo import ForgejoError
from .gitstore import ContentError, GitError
from .search import lines, outline
from .service import Denied

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
READ_BUDGET = 20_000
INSTRUCTIONS = (
    "Wiki Hub holds the signed-in person's curated knowledge: facts about them, their preferences, people, "
    "documents and plans, in the wikis they can access. Consult it whenever an answer may depend on what they "
    "have recorded rather than general knowledge, and before asking them for a fact it may already hold.\n\n"
    "Layout: wiki/ holds maintained pages and wiki/index.md is each wiki's map; reference/ holds faithful text "
    "extracted from the preserved originals in raw/.\n\n"
    "Workflow: wiki_list shows what each wiki covers. wiki_search searches every accessible wiki unless one is "
    "named; all terms must match (match=any reports a fallback to any term), \"quoted phrases\" and -exclusions "
    "work, and words are stemmed. Read around a hit with wiki_read start_line/end_line; a long file returns an "
    "outline to choose a range. Prefer dated, current statements and keep conflicting accounts attributed. Cite "
    "wiki, path and revision. Sources establish what they say, not independent verification. wiki_ingest saves "
    "a new source automatically and returns a job ID."
)


def numbered(text, first):
    return "".join(f"{number:>6}\t{line}" for number, line in enumerate(lines(text), first))


def create_mcp(service, auth=None, subject_provider=None, errors=(), *, tool_prefix="wiki_"):
    # Unexpected failures must not reveal server paths or internals to remote clients.
    mcp = FastMCP("wiki-hub", auth=auth, instructions=INSTRUCTIONS, mask_error_details=True)

    async def call(method, *args):
        try:
            subject = subject_provider() if subject_provider else principal_subject(get_access_token())
            return await getattr(service, method)(subject, *args)
        except (Denied, ContentError, GitError, ForgejoError, *errors) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(name=tool_prefix + "list", annotations=READ)
    async def wiki_list() -> list[dict]:
        """List the wikis the user can read and what each covers. Start here when unsure which wiki holds
        their facts, preferences or records. Archived wikis remain readable by their known name."""
        return await call("list_wikis")

    @mcp.tool(name=tool_prefix + "pages", annotations=READ)
    async def wiki_pages(wiki: str) -> dict:
        """Map one wiki: each page and extracted reference with its title and line count, at the accepted
        revision. Use it to browse when search terms are unclear."""
        result = await call("pages", wiki)
        files = result.get("files") or [{"path": path} for path in result.get("pages", [])]
        return {"wiki": result["wiki"], "revision": result["revision"], "files": files}

    @mcp.tool(name=tool_prefix + "search", annotations=READ)
    async def wiki_search(query: str, wiki: str | None = None, limit: int = 10) -> dict:
        """Search the user's recorded knowledge (personal facts, preferences, people, documents, plans) before
        answering from general knowledge or asking them. Searches every accessible wiki unless wiki is given.
        All terms must match, falling back to any term (match=any); "exact phrase" and -exclude work and words
        are stemmed. Hits give wiki, path, heading, numbered snippet lines and the revision to cite."""
        result = await call("search", wiki, query, limit)
        for hit in result.get("results", []):
            hit["snippet"] = numbered(hit["snippet"], hit["line"])
        return result

    @mcp.tool(name=tool_prefix + "read", annotations=READ)
    async def wiki_read(wiki: str, path: str, revision: str | None = None,
                        start_line: int | None = None, end_line: int | None = None) -> dict:
        """Read a page or extracted reference with numbered lines. Pass start_line/end_line to read around a
        search hit. Output stops near 20,000 characters, giving next_start_line and, for whole-file reads, an
        outline of headings to choose a range; line_truncated marks a single longer line cut short. Pass a
        search result's full revision for reproducible evidence."""
        ranged = start_line is not None or end_line is not None
        page = await call("read_page", wiki, path, revision, *((start_line, end_line) if ranged else ()))
        first = page.get("start_line", 1)
        content = lines(page["content"])
        taken, size = [], 0
        for line in content:
            if taken and size + len(line) > READ_BUDGET:
                break
            taken.append(line[:READ_BUDGET])
            size += len(taken[-1])
        result = {"wiki": page["wiki"], "path": page["path"], "revision": page["revision"],
                  "start_line": first, "end_line": first + len(taken) - 1,
                  "total_lines": page.get("total_lines", len(content)), "content": numbered("".join(taken), first)}
        if taken and len(taken[-1]) < len(content[len(taken) - 1]):
            # One line longer than the budget: say so rather than presenting a partial line as whole.
            result["line_truncated"] = result["end_line"]
        if len(taken) < len(content) or "line_truncated" in result:
            result["truncated"] = True
            if len(taken) < len(content):
                result["next_start_line"] = first + len(taken)
            if not ranged:
                result["outline"] = outline(page["content"])
        return result

    @mcp.tool(name=tool_prefix + "extract", annotations=READ)
    async def wiki_extract(wiki: str, filename: str, content_base64: str) -> dict:
        """Preview bounded extracted text, including OCR and email bodies, without retaining a source.
        Requires current contribute access to the named wiki. Originals are never modified."""
        return await call("extract_preview", wiki, filename, content_base64)

    @mcp.tool(name=tool_prefix + "ingest", annotations=WRITE)
    async def wiki_ingest(wiki: str, filename: str, content_base64: str, instructions: str = "",
                          submission_id: str | None = None) -> dict:
        """Submit a source. Retain submission_id; reuse after a lost reply without repeating model work."""
        return await call("ingest", wiki, filename, content_base64, instructions, submission_id)

    @mcp.tool(name=tool_prefix + "jobs", annotations=READ)
    async def wiki_jobs(wiki: str, status: str | None = None, limit: int = 50, before: str | None = None) -> dict:
        """Find retained jobs, including lost IDs and uncertain outcomes. Paginate with next_before."""
        return await call("jobs", wiki, status, limit, before)

    @mcp.tool(name=tool_prefix + "ingest_status", annotations=READ)
    async def wiki_ingest_status(job_id: str) -> dict:
        """Read an ingestion job's status, failure or resulting commit."""
        return await call("job", job_id)

    @mcp.tool(name=tool_prefix + "history", annotations=READ)
    async def wiki_history(wiki: str) -> list[dict]:
        """List recent accepted revisions for this wiki."""
        return await call("history", wiki)

    return mcp
