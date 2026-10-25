"""One tool catalog for remote OAuth clients and the signed-in stdio CLI."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from .auth import principal_subject
from .forgejo import ForgejoError
from .gitstore import ContentError, GitError
from .service import Denied

READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


def create_mcp(service, auth=None, subject_provider=None):
    mcp = FastMCP(
        "wiki-hub", auth=auth,
        instructions=(
            "Knowledge collections available to the signed-in caller. List wikis first, then search and read "
            "relevant pages or extracted references. Cite their paths and revision. Preserved sources establish "
            "what a source says, not independent verification. Ingestion saves automatically and returns a job ID."
        ),
    )

    async def call(method, *args):
        try:
            subject = subject_provider() if subject_provider else principal_subject(get_access_token())
            return await getattr(service, method)(subject, *args)
        except (Denied, ContentError, GitError, ForgejoError) as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool(annotations=READ)
    async def wiki_list() -> list[dict]:
        """List accessible active wikis. Archived wikis remain readable by their known name."""
        return await call("list_wikis")

    @mcp.tool(annotations=READ)
    async def wiki_pages(wiki: str) -> dict:
        """List a wiki's Markdown pages and accepted Git revision."""
        return await call("pages", wiki)

    @mcp.tool(annotations=READ)
    async def wiki_search(wiki: str, query: str, limit: int = 10) -> dict:
        """Find source-linked passages in one wiki; results include paths, lines and revisions."""
        return await call("search", wiki, query, limit)

    @mcp.tool(annotations=READ)
    async def wiki_read(wiki: str, path: str) -> dict:
        """Read a Markdown page or faithful extracted reference from a search result."""
        return await call("read_page", wiki, path)

    @mcp.tool(annotations=WRITE)
    async def wiki_ingest(wiki: str, filename: str, content_base64: str, instructions: str = "") -> dict:
        """Submit a base64 PDF or UTF-8 source. Saves automatically; poll the returned job ID."""
        return await call("ingest", wiki, filename, content_base64, instructions)

    @mcp.tool(annotations=READ)
    async def wiki_ingest_status(job_id: str) -> dict:
        """Read an ingestion job's status, failure or resulting commit."""
        return await call("job", job_id)

    @mcp.tool(annotations=READ)
    async def wiki_history(wiki: str) -> list[dict]:
        """List recent accepted revisions for this wiki."""
        return await call("history", wiki)

    return mcp
