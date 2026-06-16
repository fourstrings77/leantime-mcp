# SPDX-FileCopyrightText: 2025 Daniel Eder
#
# SPDX-License-Identifier: MIT

"""Leantime JSON-RPC 2.0 client implementation."""

import asyncio
import random
import httpx
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

# HTTP status codes worth retrying (rate limiting + transient upstream errors).
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


class LeantimeAPIError(Exception):
    """Exception raised for Leantime API errors."""
    
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        # Leantime carries the actionable reason in `data` (e.g. for -32602 the
        # underlying "Required Parameter Missing: x" / "Could not cast parameter:
        # y"). Surface it instead of just the generic top-level message.
        detail = f"Leantime API Error {code}: {message}"
        if data not in (None, ""):
            detail += f" — {data}"
        super().__init__(detail)


class LeantimeClient:
    """Client for interacting with Leantime's JSON-RPC 2.0 API."""
    
    def __init__(self, base_url: str, api_key: str, user_email: Optional[str] = None, max_retries: int = 4, backoff_base: float = 0.5, backoff_max: float = 30.0):
        """Initialize the Leantime client.

        Args:
            base_url: Base URL of the Leantime instance (e.g., https://leantime.example.com)
            api_key: API key for authentication
            user_email: Email of the authenticated user. Used to resolve a default
                user_id when one is not supplied (e.g. on create_ticket).
            max_retries: Maximum number of retries for transient failures
                (rate limiting / 5xx). 0 disables retrying.
            backoff_base: Base delay in seconds for exponential backoff.
            backoff_max: Cap on a single backoff delay in seconds.
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.user_email = user_email
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.endpoint = f"{self.base_url}/api/jsonrpc"
        self._request_id = 0
        self._current_user_id: Optional[int] = None
    
    def _get_next_id(self) -> int:
        """Get next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id
    
    async def call(self, method: str, params: Optional[dict] = None) -> Any:
        """Make a JSON-RPC 2.0 call to Leantime API.
        
        Args:
            method: RPC method name (e.g., "leantime.rpc.Projects.getProject")
            params: Method parameters as dictionary
            
        Returns:
            The result from the JSON-RPC response
            
        Transient failures (HTTP 429/502/503/504, network errors, and upstream
        rate-limit errors returned as JSON-RPC errors) are retried with bounded
        exponential backoff. A 429 Retry-After header, if present, is honored.

        Raises:
            LeantimeAPIError: If the API returns a non-transient error, or a
                transient one that still fails after exhausting retries.
            httpx.HTTPError: If there's a network/HTTP error that persists.
        """
        attempt = 0
        while True:
            try:
                return await self._call_once(method, params)
            except (httpx.HTTPStatusError, httpx.TransportError, LeantimeAPIError) as exc:
                retry_after = self._retry_delay(exc)
                if retry_after is None or attempt >= self.max_retries:
                    raise

                delay = retry_after if retry_after > 0 else self._backoff_delay(attempt)
                attempt += 1
                logger.warning(
                    f"Transient error calling {method} ({exc}); "
                    f"retry {attempt}/{self.max_retries} in {delay:.2f}s"
                )
                await asyncio.sleep(delay)

    async def _call_once(self, method: str, params: Optional[dict]) -> Any:
        """Perform a single JSON-RPC request (no retry)."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._get_next_id()
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key
        }

        logger.debug(f"Calling Leantime RPC: {method} with params: {params}")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=30.0
            )
            response.raise_for_status()

            data = response.json()

            # Check for JSON-RPC error
            if "error" in data:
                error = data["error"]
                raise LeantimeAPIError(
                    code=error.get("code", -1),
                    message=error.get("message", "Unknown error"),
                    data=error.get("data")
                )

            # Return the result
            return data.get("result")

    def _retry_delay(self, exc: Exception) -> Optional[float]:
        """Decide whether an exception is retryable.

        Returns a delay in seconds to wait before retrying (>= 0), where a
        positive value is a server-provided hint (Retry-After) that overrides
        backoff, and 0 means "retryable, use exponential backoff". Returns None
        if the exception should not be retried.
        """
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in RETRYABLE_STATUS_CODES:
                return self._parse_retry_after(exc.response) or 0.0
            return None

        if isinstance(exc, httpx.TransportError):
            # Network-level failures (timeouts, connection resets) are transient.
            return 0.0

        if isinstance(exc, LeantimeAPIError):
            # Some upstreams surface rate limiting as a JSON-RPC error rather
            # than an HTTP 429.
            message = (exc.message or "").lower()
            if "rate limit" in message or "too many requests" in message:
                return 0.0
            return None

        return None

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> Optional[float]:
        """Parse a Retry-After header (delta-seconds form) if present."""
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            # HTTP-date form is uncommon for this API; fall back to backoff.
            return None

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at backoff_max."""
        ceiling = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return random.uniform(0.0, ceiling)
    
    # Convenience methods for common operations
    
    async def get_project(self, project_id: int) -> dict:
        """Get project details by ID."""
        return await self.call("leantime.rpc.Projects.getProject", {"id": project_id})
    
    async def list_projects(self) -> list:
        """List all projects."""
        return await self.call("leantime.rpc.Projects.getAll")
    
    async def create_project(self, name: str, details: Optional[str] = None, **kwargs) -> dict:
        """Create a new project."""
        params = {"name": name, **kwargs}
        if details:
            params["details"] = details
        return await self.call("leantime.rpc.Projects.addProject", params)
    
    async def get_ticket(self, ticket_id: int) -> dict:
        """Get ticket details by ID."""
        return await self.call("leantime.rpc.Tickets.Tickets.getTicket", {"id": ticket_id})
    
    async def list_tickets(
        self,
        project_id: Optional[int] = None,
        status: Optional[str] = None,
        ticket_type: Optional[str] = None,
        priority: Optional[str] = None,
        milestone: Optional[str] = None,
        term: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list:
        """List tickets with optional server-side filters.

        Args:
            project_id: Restrict to a project (searchCriteria.currentProject).
            status: Comma-separated status IDs, or one of 'all', 'not_done',
                'done' (searchCriteria.status).
            ticket_type: Comma-separated ticket types (searchCriteria.type).
            priority: Comma-separated priorities (searchCriteria.priority).
            milestone: Comma-separated milestone IDs (searchCriteria.milestone).
            term: Free-text search term (searchCriteria.term).
            limit: Maximum number of tickets to return (passed to getAll).
        """
        searchCriteria: dict = {}
        if project_id:
            searchCriteria["currentProject"] = project_id
        if status is not None:
            searchCriteria["status"] = status
        if ticket_type is not None:
            searchCriteria["type"] = ticket_type
        if priority is not None:
            searchCriteria["priority"] = priority
        if milestone is not None:
            searchCriteria["milestone"] = milestone
        if term is not None:
            searchCriteria["term"] = term

        params: dict = {"searchCriteria": searchCriteria}
        if limit is not None:
            params["limit"] = limit
        return await self.call("leantime.rpc.Tickets.Tickets.getAll", params)

    async def get_files_by_module(self, module: str, module_id: int) -> list:
        """List files/attachments for a module entity (e.g. a ticket).

        Read-only. Uploading is not available over JSON-RPC: the Files service's
        upload() needs a web ($_FILES) multipart request with a server-side temp
        file, which RPC cannot supply.
        """
        params = {"module": module, "entityId": module_id}
        return await self.call("leantime.rpc.Files.getFilesByModule", params)

    async def create_ticket(self, headline: str, project_id: int, user_id: Optional[int] = None, date: Optional[str] = None, tags: Optional[str] = None, milestone_id: Optional[int] = None, **kwargs) -> dict:
        """Create a new ticket.

        Args:
            headline: Title/headline of the ticket
            project_id: Project ID where the ticket will be created
            user_id: The ID of the user creating the ticket. If omitted, defaults to
                the authenticated user resolved from the configured email.
            date: The date when the ticket is created (YYYY-MM-DD format). Defaults to current date if not provided.
            tags: Comma-separated list of tags to add to the ticket
            milestone_id: ID of the milestone to associate the ticket with
            **kwargs: Additional parameters
        """
        from datetime import datetime

        # Use current date if none provided
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # Default to the authenticated user when no user_id is supplied
        if user_id is None:
            user_id = await self.resolve_current_user_id()

        # The API expects a 'values' parameter containing the ticket data
        values = {
            "headline": headline,
            "projectId": project_id,
            "userId": user_id,
            "date": date,
            **kwargs
        }

        # Add tags if provided
        if tags is not None:
            values["tags"] = tags

        # Add milestone if provided (the API field is 'milestoneid')
        if milestone_id is not None:
            values["milestoneid"] = milestone_id

        params = {"values": values}
        return await self.call("leantime.rpc.Tickets.Tickets.addTicket", params)
    
    async def update_ticket(self, ticket_id: int, project_id: int, merge: bool = True, **kwargs) -> dict:
        """Update an existing ticket.

        Leantime's updateTicket rebuilds the ticket from the supplied values and
        blanks any field that is not sent (there is no PATCH endpoint). To avoid
        silent data loss, this method fetches the current ticket and merges the
        provided fields over it, so unspecified fields are preserved.

        Args:
            ticket_id: The ID of the ticket to update
            project_id: The project ID where the ticket belongs
            merge: When True (default), fetch the existing ticket and merge the
                provided fields over it. Set False to send only the provided
                fields (legacy destructive behavior).
            **kwargs: Fields to update (headline, description, status, tags,
                milestoneid, priority, assignedTo, ...)
        """
        values: dict = {}

        if merge:
            current = await self.get_ticket(ticket_id)
            if isinstance(current, dict):
                # Carry forward the existing ticket as the baseline. updateTicket
                # whitelists the keys it consumes, so extra/computed fields are
                # harmless; the point is to preserve description/tags/milestone/etc.
                values.update(current)

        # Applied last so caller-provided fields win over the fetched baseline.
        values.update(kwargs)

        # Always pin identity fields regardless of what the baseline contained.
        values["id"] = ticket_id
        values["projectId"] = project_id

        params = {"values": values}
        return await self.call("leantime.rpc.Tickets.Tickets.updateTicket", params)
    
    async def get_status_labels(self) -> dict:
        """Get all available ticket status labels with their IDs.
        
        Returns:
            A dictionary mapping status IDs to their labels
        """
        return await self.call("leantime.rpc.Tickets.Tickets.getStatusLabels")
    
    async def get_user(self, user_id: int) -> dict:
        """Get user details by ID."""
        return await self.call("leantime.rpc.Users.getUser", {"id": user_id})
    
    async def list_users(self) -> list:
        """List all users."""
        return await self.call("leantime.rpc.Users.getAll")
    
    async def get_user_by_email(self, email: str) -> dict:
        """Get user details by email address."""
        return await self.call("leantime.rpc.Users.Users.getUserByEmail", {"email": email})

    async def resolve_current_user_id(self) -> int:
        """Resolve the user_id of the authenticated user from the configured email.

        The result is cached for the lifetime of the client. Raises ValueError if
        no email is configured or the user cannot be found.
        """
        if self._current_user_id is not None:
            return self._current_user_id

        if not self.user_email:
            raise ValueError(
                "No user_id was provided and no user email is configured "
                "(set LEANTIME_USER_EMAIL) to resolve a default user."
            )

        user = await self.get_user_by_email(self.user_email)
        user_id = user.get("id") if isinstance(user, dict) else None
        if not user_id:
            raise ValueError(
                f"Could not resolve a user_id for email '{self.user_email}'."
            )

        self._current_user_id = int(user_id)
        return self._current_user_id
    
    async def add_comment(self, module: str, module_id: int, comment: str) -> dict:
        """Add a comment to a module (e.g., ticket, project).

        The signature is addComment($values, $module, $entityId, $entity), where
        $values is an array keyed by 'text'. The previous flat
        {module, moduleId, comment} shape produced "Invalid params".

        `entity` is sent explicitly as null and `father` as 0 for cross-version
        portability: older Leantime (<=3.7.x) requires both `entity` and
        `values['father']` to be present (no self-load, no father default) — see
        the guard in Comments::addComment. Leantime >=3.9 supplies both defaults
        and self-loads the entity, so the explicit values are harmless there.

        NOTE: on Leantime 3.7.x the comment row is inserted but the call then
        throws because the notification assigns the (session-less, hence null)
        currentProject to a non-nullable `int $projectId`. Clean RPC comments
        require Leantime >= 3.9.
        """
        params = {
            "values": {"text": comment, "father": 0},
            "module": module,
            "entityId": module_id,
            "entity": None,
        }
        return await self.call("leantime.rpc.Comments.addComment", params)

    async def get_comments(self, module: str, module_id: int) -> list:
        """Get comments for a module.

        Upstream signature: getComments($module, $entityId, $commentOrder=0, $parent=0).
        """
        params = {
            "module": module,
            "entityId": module_id,
        }
        return await self.call("leantime.rpc.Comments.getComments", params)
    
    async def add_timesheet(self, user_id: int, ticket_id: int, hours: float, date: str, **kwargs) -> dict:
        """Add a timesheet entry."""
        params = {
            "userId": user_id,
            "ticketId": ticket_id,
            "hours": hours,
            "date": date,
            **kwargs
        }
        return await self.call("leantime.rpc.Timesheets.addTime", params)
    
    async def get_timesheets(self, project_id: Optional[int] = None, user_id: Optional[int] = None) -> list:
        """Get timesheet entries."""
        params = {}
        if project_id:
            params["projectId"] = project_id
        if user_id:
            params["userId"] = user_id
        return await self.call("leantime.rpc.Timesheets.getTimesheets", params)
    
    async def get_all_subtasks(self, ticket_id: int) -> list:
        """Get all subtasks for a ticket.
        
        Args:
            ticket_id: The ID of the parent ticket
            
        Returns:
            A list of subtasks or false if an error occurred
        """
        params = {"ticketId": ticket_id}
        return await self.call("leantime.rpc.Tickets.Tickets.getAllSubtasks", params)
    
    async def upsert_subtask(self, parent_ticket_id: int, headline: str, date: Optional[str] = None, tags: Optional[str] = None, **kwargs) -> dict:
        """Create or update a subtask.
        
        Args:
            parent_ticket_id: The ID of the parent ticket
            headline: Title/headline of the subtask
            date: The date when the subtask is created (YYYY-MM-DD format). Defaults to current date if not provided.
            tags: Comma-separated list of tags to add to the subtask
            **kwargs: Additional parameters (description, status, priority, assignedTo, etc.)
            
        Returns:
            The created subtask data
        """
        from datetime import datetime
        
        # Use current date if none provided
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        
        # Fetch the parent ticket data to get project_id and milestone_id
        parent_ticket_data = await self.get_ticket(parent_ticket_id)
        
        if not parent_ticket_data:
            raise ValueError(f"Parent ticket with ID {parent_ticket_id} not found")
        
        # Extract required fields from parent ticket
        project_id = parent_ticket_data.get("projectId")
        if not project_id:
            raise ValueError(f"Could not determine projectId from parent ticket {parent_ticket_id}")
        
              # Extract required fields from parent ticket
        user_id = parent_ticket_data.get("userId")
        if not user_id:
            raise ValueError(f"Could not determine userId from parent ticket {parent_ticket_id}")

        milestone_id = parent_ticket_data.get("milestoneid")
        
        # The API expects a 'values' parameter containing the subtask data
        values = {
            "headline": headline,
            "type": "subtask",  # Mark this as a subtask
            "projectId": project_id,
            "userId": user_id,
            "date": date,
            "dependingTicketId": parent_ticket_id,  # Link to parent ticket
            "milestoneid": milestone_id if milestone_id else "",  # Use parent's milestone
            **kwargs
        }
        
        # Add tags if provided
        if tags is not None:
            values["tags"] = tags
        
        # Use addTicket to create the subtask
        params = {"values": values}
        
        # Debug logging
        logger.info(f"Creating subtask via addTicket: type=subtask, dependingTicketId={parent_ticket_id}, milestoneid={milestone_id}")
        
        return await self.call("leantime.rpc.Tickets.Tickets.addTicket", params)
