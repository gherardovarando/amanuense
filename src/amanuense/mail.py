"""Mailbox access through an IMAP MCP server, restricted to the tools a run needs."""
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_TOOLS = ["imap_search_emails", "imap_get_email"]


class MailError(RuntimeError):
    pass


class Mailbox:
    """Async context manager. With write=True the server also exposes the keyword tools
    (add/remove custom IMAP keywords); sending, moving, flagging and deleting are never enabled."""

    def __init__(self, cfg, write=False, verbose=False):
        self.cfg = cfg
        self.verbose = verbose
        self.account = cfg["mail"]["account"]
        tools = READ_TOOLS + (["imap_add_keyword", "imap_remove_keyword"] if write else [])
        mcp = cfg["mcp"]
        command = os.path.expanduser(mcp["command"])
        if not shutil.which(command):
            raise MailError(f"MCP server command not found: {command} (set mcp.command)")
        env = {**{k: str(v) for k, v in mcp.get("env", {}).items()}, "IMAP_MCP_ENABLED_TOOLS": ",".join(tools)}
        self.params = StdioServerParameters(
            command=command, args=[os.path.expanduser(a) for a in mcp["args"]], env=env)

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        # The server's own startup messages go to stderr only with --verbose.
        errlog = sys.stderr if self.verbose else self._stack.enter_context(open(os.devnull, "w"))
        r, w = await self._stack.enter_async_context(stdio_client(self.params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(r, w))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _call(self, tool, **args):
        if self.account:
            args["accountName"] = self.account
        res = await self.session.call_tool(tool, args)
        if getattr(res, "is_error", None) or getattr(res, "isError", None):
            text = res.content[0].text if res.content else ""
            raise MailError(f"{tool} failed: {text[:200]}")
        return json.loads(res.content[0].text)

    async def search(self, folder, **criteria):
        return (await self._call("imap_search_emails", folder=folder, **criteria))["messages"]

    async def search_all(self, **criteria):
        """Search every folder except Trash, Spam and Drafts; each message gets a "folder" field."""
        return (await self._call("imap_search_emails", searchAllFolders=True, **criteria))["messages"]

    async def get(self, folder, uid, body_chars):
        e = (await self._call("imap_get_email", folder=folder, uid=uid, bodyFormat="text",
                              maxContentLength=body_chars, includeAttachmentText=False))["email"]
        return {
            "body": e.get("textContent") or e.get("markdownContent") or "",
            "attachments": [a.get("filename") for a in e.get("attachments") or [] if a.get("filename")],
        }

    async def add_keyword(self, folder, uid, keyword):
        await self._call("imap_add_keyword", folder=folder, uid=uid, keyword=keyword)

    async def remove_keyword(self, folder, uid, keyword):
        await self._call("imap_remove_keyword", folder=folder, uid=uid, keyword=keyword)
