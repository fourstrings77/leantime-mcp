# SPDX-FileCopyrightText: 2025 Daniel Eder
#
# SPDX-License-Identifier: MIT

"""Leantime MCP Server - Main server implementation."""

import os
import sys
import json
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


@app.tool()
async def create_ticket(headline: str, project_id: int, user_id: int = None, date: str = None,
                       description: str = None, status: int | str = None, priority: str = None,
                       assignedTo: int = None, tags: str = None, milestone_id: int = None,
                       depends_on: int = None) -> str:
    """Create a new ticket.

    If user_id is omitted, the ticket is created as the authenticated user
    (resolved from the configured LEANTIME_USER_EMAIL).

    status accepts an integer id or a canonical name (new, in_progress, blocked,
    waiting, done, archived). depends_on sets the parent/dependency ticket
    (Leantime's dependingTicketId).
    """
    client = get_client()
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
    return json.dumps(result, indent=2)


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
