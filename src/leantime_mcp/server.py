# SPDX-FileCopyrightText: 2025 Daniel Eder
#
# SPDX-License-Identifier: MIT

"""Leantime MCP Server - Main server implementation."""

import os
import sys
import re
import json
import difflib
import logging
from typing import Any
from dotenv import load_dotenv

from fastmcp import FastMCP

from leantime_mcp.client import LeantimeClient, LeantimeAPIError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Initialize the FastMCP server
app = FastMCP("leantime-mcp")

# Global Leantime client instance
leantime_client: LeantimeClient = None


def get_client() -> LeantimeClient:
    """Get or create the Leantime client instance."""
    global leantime_client
    
    if leantime_client is None:
        # Get configuration from environment
        leantime_url = os.getenv("LEANTIME_URL")
        leantime_api_key = os.getenv("LEANTIME_API_KEY")
        leantime_user_email = os.getenv("LEANTIME_USER_EMAIL")
        
        if not leantime_url:
            raise ValueError(
                "LEANTIME_URL environment variable is required. "
                "Please set it in your .env file or environment."
            )
        
        if not leantime_api_key:
            raise ValueError(
                "LEANTIME_API_KEY environment variable is required. "
                "Please set it in your .env file or environment."
            )
        
        if not leantime_user_email:
            raise ValueError(
                "LEANTIME_USER_EMAIL environment variable is required. "
                "Please set it in your .env file or environment."
            )
        
        leantime_client = LeantimeClient(leantime_url, leantime_api_key, leantime_user_email)
        logger.info(f"Initialized Leantime client for {leantime_url}")
    
    return leantime_client


# Tool functions will be defined below


# Canonical status name -> Leantime status id. The integer mapping is non-obvious
# and non-monotonic (0=Done sits next to 1=Blocked), so tools accept these names
# in addition to raw ints. Labels in the UI are localized, so we map by canonical
# English key rather than the displayed label.
STATUS_NAME_TO_ID = {
    "new": 3,
    "blocked": 1,
    "in_progress": 4,
    "waiting": 2,
    "waiting_for_confirmation": 2,
    "done": 0,
    "archived": -1,
}


def _resolve_status(status) -> int:
    """Normalize a status given as an int, a numeric string, or a canonical name.

    Returns the integer status id. Raises ValueError on an unknown name.
    """
    if isinstance(status, bool):  # guard: bool is an int subclass
        raise ValueError("status must be an int or a status name, not a bool")
    if isinstance(status, int):
        return status
    s = str(status).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    key = s.lower().replace(" ", "_").replace("-", "_")
    if key in STATUS_NAME_TO_ID:
        return STATUS_NAME_TO_ID[key]
    raise ValueError(
        f"Unknown status {status!r}. Use an integer id or one of: "
        + ", ".join(sorted(set(STATUS_NAME_TO_ID)))
    )



@app.tool()
async def get_project(project_id: int) -> str:
    """Get details of a specific project by ID."""
    client = get_client()
    result = await client.get_project(project_id)
    return json.dumps(result, indent=2)


@app.tool()
async def list_projects() -> str:
    """List all projects accessible to the user."""
    client = get_client()
    result = await client.list_projects()
    return json.dumps(result, indent=2)


@app.tool()
async def create_project(name: str, details: str = None, clientId: int = None) -> str:
    """Create a new project."""
    client = get_client()
    result = await client.create_project(name=name, details=details, clientId=clientId)
    return json.dumps(result, indent=2)


@app.tool()
async def get_ticket(ticket_id: int) -> str:
    """Get details of a specific ticket by ID."""
    client = get_client()
    result = await client.get_ticket(ticket_id)
    return json.dumps(result, indent=2)


# Fields returned in compact list mode — a lightweight board index that stays
# well under the tool-result token cap (full ticket objects include rich-HTML
# descriptions that overflow it).
COMPACT_TICKET_FIELDS = [
    "id", "headline", "type", "status", "priority",
    "tags", "milestoneid", "editorId", "dateToFinish", "dependingTicketId",
]


@app.tool()
async def list_tickets(project_id: int = None, status: str = None, ticket_type: str = None,
                       priority: str = None, milestone: str = None, term: str = None,
                       limit: int = None, compact: bool = True, fields: str = None) -> str:
    """List tickets with optional filters.

    Returns a compact index by default (id, headline, type, status, priority,
    tags, milestoneid, editorId, dateToFinish, dependingTicketId) to stay under
    the tool-result token limit — full ticket objects include rich-HTML
    descriptions that overflow it. Use get_ticket for a single full ticket.

    Filters (all optional): status accepts comma-separated status IDs or one of
    'all'/'not_done'/'done'; ticket_type, priority, milestone accept
    comma-separated values; term is free-text search; limit caps the count.

    Set compact=False for full ticket objects, or pass fields as a
    comma-separated list to choose exactly which fields to return.
    """
    client = get_client()
    result = await client.list_tickets(
        project_id=project_id, status=status, ticket_type=ticket_type,
        priority=priority, milestone=milestone, term=term, limit=limit
    )

    if fields:
        selected = [f.strip() for f in fields.split(",") if f.strip()]
    elif compact:
        selected = COMPACT_TICKET_FIELDS
    else:
        selected = None

    if selected is not None and isinstance(result, list):
        result = [
            {k: t[k] for k in selected if k in t}
            for t in result if isinstance(t, dict)
        ]

    return json.dumps(result, indent=2)


async def _find_duplicate_headlines(client, project_id: int, headline: str, threshold: float) -> list:
    """Return existing tickets whose headline is similar to `headline`.

    Compares case-insensitively with difflib; archived tickets (status -1) are
    ignored. Sorted by descending similarity.
    """
    existing = await client.list_tickets(project_id)
    if not isinstance(existing, list):
        return []
    target = headline.strip().lower()
    candidates = []
    for t in existing:
        if not isinstance(t, dict) or t.get("status") == -1:
            continue
        other = str(t.get("headline", "")).strip().lower()
        if not other:
            continue
        ratio = difflib.SequenceMatcher(None, target, other).ratio()
        if ratio >= threshold:
            candidates.append({
                "id": t.get("id"),
                "headline": t.get("headline"),
                "status": t.get("status"),
                "similarity": round(ratio, 3),
            })
    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    return candidates


@app.tool()
async def create_ticket(headline: str, project_id: int, user_id: int = None, date: str = None,
                       description: str = None, status: int | str = None, priority: str = None,
                       assignedTo: int = None, tags: str = None, milestone_id: int = None,
                       depends_on: int = None, confirm_create: bool = False,
                       dedupe_threshold: float = 0.85) -> str:
    """Create a new ticket.

    If user_id is omitted, the ticket is created as the authenticated user
    (resolved from the configured LEANTIME_USER_EMAIL).

    status accepts an integer id or a canonical name (new, in_progress, blocked,
    waiting, done, archived). depends_on sets the parent/dependency ticket
    (Leantime's dependingTicketId).

    Dedupe guardrail: before creating, the new headline is fuzzy-matched against
    the project's existing (non-archived) tickets. If any are at or above
    dedupe_threshold (0..1, default 0.85), creation is REFUSED and the matches
    are returned. Pass confirm_create=true to create anyway.
    """
    client = get_client()

    if not confirm_create:
        candidates = await _find_duplicate_headlines(client, project_id, headline, dedupe_threshold)
        if candidates:
            return json.dumps({
                "created": False,
                "reason": "possible duplicate headline(s) found",
                "candidates": candidates[:5],
                "hint": "pass confirm_create=true to create anyway, or update an existing ticket",
            }, indent=2)

    kwargs = {}
    if depends_on is not None:
        kwargs['dependingTicketId'] = depends_on
    result = await client.create_ticket(
        headline=headline, project_id=project_id, user_id=user_id, date=date,
        description=description,
        status=_resolve_status(status) if status is not None else None,
        priority=priority, assignedTo=assignedTo, tags=tags,
        milestone_id=milestone_id, **kwargs
    )
    return json.dumps({"created": True, "result": result}, indent=2)


@app.tool()
async def update_ticket(ticket_id: int, project_id: int, headline: str = None, description: str = None,
                       status: int | str = None, priority: str = None, assignedTo: int = None,
                       tags: str = None, milestone_id: int = None, depends_on: int = None,
                       comment: str = None) -> str:
    """Update an existing ticket (partial update).

    Only the fields you provide are changed; all other fields are preserved.
    The server has no PATCH endpoint, so this fetches the current ticket and
    merges your changes over it before saving. Tags and milestone are settable
    here (the API supports both on write).

    status accepts an integer id or a canonical name (new, in_progress, blocked,
    waiting, done, archived). depends_on sets the parent/dependency ticket
    (dependingTicketId). comment, if given, is added to the ticket after the
    update — handy for logging a transition ("moved to done: MR merged") to the
    audit trail instead of mutating the description.
    """
    client = get_client()
    # Build kwargs from non-None parameters
    kwargs = {}
    if headline is not None:
        kwargs['headline'] = headline
    if description is not None:
        kwargs['description'] = description
    if status is not None:
        kwargs['status'] = _resolve_status(status)
    if priority is not None:
        kwargs['priority'] = priority
    if assignedTo is not None:
        kwargs['assignedTo'] = assignedTo
    if tags is not None:
        kwargs['tags'] = tags
    if milestone_id is not None:
        # The API field is 'milestoneid'
        kwargs['milestoneid'] = milestone_id
    if depends_on is not None:
        kwargs['dependingTicketId'] = depends_on

    result = await client.update_ticket(ticket_id, project_id, **kwargs)

    response = {"updated": result}
    if comment is not None:
        response["comment_added"] = await client.add_comment("ticket", ticket_id, comment)

    return json.dumps(response, indent=2)


@app.tool()
async def bulk_update_status(ticket_ids: list[int], status: int | str, project_id: int) -> str:
    """Move several tickets to the same status in one call.

    Loops over update_ticket (which fetch-merges, so each ticket's other fields
    are preserved) and reports per-ticket success/failure instead of aborting on
    the first error. status accepts an integer id or a canonical name.
    """
    client = get_client()
    resolved = _resolve_status(status)
    results = []
    for tid in ticket_ids:
        try:
            await client.update_ticket(tid, project_id, status=resolved)
            results.append({"ticket_id": tid, "success": True})
        except Exception as exc:  # per-ticket resilience: keep going on failure
            results.append({"ticket_id": tid, "success": False, "error": str(exc)})
    succeeded = sum(1 for r in results if r["success"])
    return json.dumps({
        "summary": {"total": len(results), "succeeded": succeeded, "failed": len(results) - succeeded},
        "results": results,
    }, indent=2)


@app.tool()
async def get_ticket_tree(ticket_id: int, max_depth: int = 5) -> str:
    """Return a ticket with its subtask hierarchy nested inline.

    Uses getAllSubtasks recursively and projects each node to the compact field
    set (same as list_tickets) so the result stays under the token limit. Each
    node carries a `children` list when it has subtasks. Cycles and depth beyond
    max_depth are guarded.
    """
    client = get_client()
    visited: set = set()

    async def build(tid: int, ticket_obj: dict, depth: int) -> dict:
        node = {k: ticket_obj[k] for k in COMPACT_TICKET_FIELDS if k in ticket_obj}
        visited.add(tid)
        if depth < max_depth:
            subs = await client.get_all_subtasks(tid)
            children = []
            if isinstance(subs, list):
                for ch in subs:
                    if not isinstance(ch, dict):
                        continue
                    cid = ch.get("id")
                    if cid is None or cid in visited:
                        continue
                    children.append(await build(cid, ch, depth + 1))
            if children:
                node["children"] = children
        return node

    root = await client.get_ticket(ticket_id)
    if not isinstance(root, dict):
        return json.dumps({"error": f"ticket {ticket_id} not found"}, indent=2)
    tree = await build(ticket_id, root, 0)
    return json.dumps(tree, indent=2)


# --- External reference links (stored as structured comments) -------------------
# Leantime tickets have no url/reference field, so links live in comments. We write
# a single-line, marker-prefixed, key=value record so it is both clickable in the
# UI (Leantime auto-links the URL) and round-trippable back into structured data.
LINK_MARKER = "🔗 LINK"
LINK_TYPES = ("merge_request", "commit", "pipeline", "branch", "issue", "doc")


def _format_link_comment(link_type: str, url: str, label: str, state: str = None) -> str:
    parts = [LINK_MARKER, f"type={link_type}", f"label={label}"]
    if state:
        parts.append(f"state={state}")
    parts.append(f"url={url}")  # url last so its value runs to end-of-line
    return " | ".join(parts)


def _parse_link_comment(text: str) -> dict | None:
    """Parse a link record out of a comment's text, or None if it isn't one."""
    if not text or LINK_MARKER not in text:
        return None
    # Leantime may wrap the URL in an <a> tag on storage; strip tags first.
    plain = re.sub(r"<[^>]+>", "", text)

    def field(name: str) -> str | None:
        m = re.search(rf"{name}=([^|]*)", plain)
        return m.group(1).strip() if m else None

    url = field("url")
    if url:
        m = re.search(r"https?://\S+", url)
        if m:
            url = m.group(0)
    return {
        "type": field("type"),
        "label": field("label"),
        "state": field("state") or None,
        "url": url,
    }


@app.tool()
async def link_ticket(ticket_id: int, type: str, url: str, label: str = None, state: str = None) -> str:
    """Attach an external reference (MR / commit / pipeline / ...) to a ticket.

    Tickets have no native link field, so the reference is written as a single
    structured comment that Leantime renders with a clickable URL and a
    timestamp, and that get_ticket_links can parse back into structured data.

    type must be one of: merge_request, commit, pipeline, branch, issue, doc.
    label is a short human tag (e.g. "!80", "abc1234", "#3"); defaults to the URL.
    state carries status where it applies (e.g. pipeline: green/red, mr: merged/open).
    """
    if type not in LINK_TYPES:
        raise ValueError(f"type must be one of: {', '.join(LINK_TYPES)}")
    client = get_client()
    label = label or url
    comment = _format_link_comment(type, url, label, state)
    added = await client.add_comment("ticket", ticket_id, comment)
    return json.dumps({
        "linked": added,
        "ticket_id": ticket_id,
        "link": {"type": type, "label": label, "state": state, "url": url},
    }, indent=2)


@app.tool()
async def get_ticket_links(ticket_id: int) -> str:
    """Return the structured external links previously attached via link_ticket.

    Parses the ticket's comments back into records {type, label, state, url}, so
    "what MR shipped this ticket?" is a query rather than reading prose.
    """
    client = get_client()
    comments = await client.get_comments("ticket", ticket_id)
    links = []
    if isinstance(comments, list):
        for c in comments:
            if not isinstance(c, dict):
                continue
            parsed = _parse_link_comment(c.get("text", ""))
            if parsed:
                parsed["comment_id"] = c.get("id")
                parsed["date"] = c.get("date")
                links.append(parsed)
    return json.dumps({"ticket_id": ticket_id, "links": links}, indent=2)


@app.tool()
async def get_ticket_files(ticket_id: int) -> str:
    """List the files/attachments on a ticket.

    Read-only. NOTE: uploading a file to a ticket is NOT possible via this MCP —
    Leantime's Files.upload() requires a web (multipart/$_FILES) request with a
    server-side temp file, which JSON-RPC cannot supply. Attach files in the
    Leantime UI; this tool lets you see what's already attached.
    """
    client = get_client()
    result = await client.get_files_by_module("ticket", ticket_id)
    return json.dumps(result, indent=2)


@app.tool()
async def get_status_labels() -> str:
    """Get available status labels."""
    client = get_client()
    result = await client.get_status_labels()
    return json.dumps(result, indent=2)


@app.tool()
async def get_user(user_id: int) -> str:
    """Get details of a specific user by ID."""
    client = get_client()
    result = await client.get_user(user_id)
    return json.dumps(result, indent=2)


@app.tool()
async def list_users() -> str:
    """List all users."""
    client = get_client()
    result = await client.list_users()
    return json.dumps(result, indent=2)


@app.tool()
async def add_comment(module: str, module_id: int, comment: str) -> str:
    """Add a comment to a module (ticket, project, etc.)."""
    client = get_client()
    result = await client.add_comment(module=module, module_id=module_id, comment=comment)
    return json.dumps(result, indent=2)


@app.tool()
async def get_comments(module: str, module_id: int) -> str:
    """Get comments for a module (ticket, project, etc.)."""
    client = get_client()
    result = await client.get_comments(module=module, module_id=module_id)
    return json.dumps(result, indent=2)


@app.tool()
async def add_timesheet(user_id: int, ticket_id: int, hours: float, date: str, description: str = None) -> str:
    """Add a timesheet entry."""
    client = get_client()
    result = await client.add_timesheet(
        user_id=user_id, ticket_id=ticket_id, hours=hours, date=date, description=description
    )
    return json.dumps(result, indent=2)


@app.tool()
async def get_timesheets(project_id: int = None, user_id: int = None) -> str:
    """Get timesheets, optionally filtered by project or user."""
    client = get_client()
    result = await client.get_timesheets(project_id=project_id, user_id=user_id)
    return json.dumps(result, indent=2)


@app.tool()
async def get_all_subtasks(ticket_id: int) -> str:
    """Get all subtasks for a ticket."""
    client = get_client()
    result = await client.get_all_subtasks(ticket_id)
    return json.dumps(result, indent=2)


@app.tool()
async def upsert_subtask(parent_ticket: int, headline: str,
                        date: str = None, description: str = None, status: str = None,
                        priority: str = None, assignedTo: str = None, tags: str = None) -> str:
    """Create or update a subtask."""
    client = get_client()
    result = await client.upsert_subtask(
        parent_ticket_id=parent_ticket, headline=headline,
        date=date, description=description, status=status, priority=priority,
        assignedTo=assignedTo, tags=tags
    )
    return json.dumps(result, indent=2)


def main():
    """Main entry point for the MCP server."""
    app.run()


if __name__ == "__main__":
    main()
