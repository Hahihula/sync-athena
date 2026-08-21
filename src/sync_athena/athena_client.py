"""Thin sync HTTP client for the Athena REST API.

Only the surface area `sync-athena` actually needs:

    - get_project_root(project_uuid, db_path)            -> Node
    - list_children(parent_id, db_path)                 -> list[Node]
    - find_child_named(parent_id, name, db_path)        -> Node | None
    - create_child_folder(parent_id, name, db_path)     -> Node
    - search_tasks(hashtag, db_path)                    -> list[Node]
    - create_task(parent_id, name, description, db_path, *, status, author, hashtags)
                                                          -> Node
    - create_child_note(parent_id, name, description, db_path, *, author, node_type)
                                                          -> Node
    - get_collaborators(node_id, db_path)               -> Collaborators
    - set_collaborators(node_id, editors, co_authors, db_path)
    - create_shortlink(node_id, db_path)                -> weblink URL

Async would mirror athena-client, but a GitHub Actions script makes a handful
of sequential calls; sync keeps the entrypoint trivial. ``httpx.Client`` with
``verify=False`` because the production Athena server uses a self-signed cert,
matching athena-client's behaviour (``src/athena/client/client.py:154``).

The error model is intentionally narrow: every non-2xx becomes ``AthenaError``
with the HTTP status and body attached, so the workflow can ``::warning::`` and
exit 0 without inspecting httpx internals.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx


class AthenaError(RuntimeError):
    """Raised on any non-2xx response from the Athena REST API."""

    def __init__(self, status_code: int, url: str, body: str) -> None:
        super().__init__(f"Athena API {status_code} for {url}: {body[:200]}")
        self.status_code = status_code
        self.url = url
        self.body = body


@dataclass
class Node:
    """Subset of the Athena node shape we need. Extra fields are ignored."""

    id: str
    name: str
    type: str = ""
    description: str = ""
    parent_node_id: str | None = None
    has_children: bool = False
    status: str | None = None
    author: str = ""
    hashtags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Node:
        return cls(
            id=str(data["id"]),
            name=data.get("name", ""),
            type=data.get("type", ""),
            description=data.get("description", "") or "",
            parent_node_id=(
                str(data["parent_node_id"])
                if data.get("parent_node_id") is not None
                else None
            ),
            has_children=bool(data.get("has_children", False)),
            status=data.get("status"),
            author=data.get("author") or "",
            hashtags=list(data.get("hashtags") or []),
            raw=data,
        )


@dataclass
class Collaborators:
    """Response shape of ``GET/PUT /api/nodes/{id}/collaborators``.

    ``manageable`` is the server telling us whether the *calling* token may
    change these lists — only the node author and project admins may. A
    false value is the usual reason a co-author assignment silently fails.
    """

    author: str = ""
    editors: list[str] = field(default_factory=list)
    co_authors: list[str] = field(default_factory=list)
    manageable: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Collaborators:
        def emails(raw: Any) -> list[str]:
            return [e["email"] if isinstance(e, dict) else e for e in (raw or [])]

        return cls(
            author=data.get("author") or "",
            editors=emails(data.get("editors")),
            co_authors=emails(data.get("co_authors")),
            manageable=bool(data.get("manageable", False)),
        )


class AthenaClient:
    """Sync HTTP client. Use as a context manager."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 30.0,
    ) -> None:
        if "://" not in base_url:
            base_url = "https://" + base_url
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            verify=False,
            headers={"Authorization": f"Bearer {token}"},
        )

    def __enter__(self) -> AthenaClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(
            method,
            path,
            params=params or None,
            json=json_body,
        )
        if not (200 <= response.status_code < 300):
            raise AthenaError(response.status_code, path, response.text)
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise AthenaError(
                response.status_code, path, f"non-JSON body: {response.text[:200]}"
            ) from exc

    def get_project_root(self, *, db_path: str) -> Node:
        """Fetch the actual root node of a project via the tree endpoint.

        The project UUID is only used to construct the db_path — the root
        node has its own distinct UUID returned by /api/nodes/tree with
        parent_id=null.
        """
        data = self._request(
            "GET", "/api/nodes/tree", params={"parent_id": "null", "_db_path": db_path}
        )
        for node in data.get("tree", []):
            if str(node.get("type", "")).upper() == "PROJECT":
                return Node.from_api(node)
        raise AthenaError(404, "/api/nodes/tree", "No PROJECT-typed root node found in tree")

    def list_children(self, *, parent_id: str, db_path: str) -> list[Node]:
        """Return the direct children of ``parent_id``.

        This is the duplicate-detection primitive: unlike
        ``advanced_search`` it reads the tree directly, so it cannot miss a
        node because a hashtag write failed or a search index lagged.
        """
        data = self._request(
            "GET",
            "/api/nodes/tree",
            params={"parent_id": parent_id, "_db_path": db_path},
        )
        return [Node.from_api(raw) for raw in data.get("tree", []) or []]

    def find_child_named(
        self, *, parent_id: str, name: str, db_path: str
    ) -> Node | None:
        """Walk direct children of ``parent_id`` looking for ``name``."""
        for node in self.list_children(parent_id=parent_id, db_path=db_path):
            if node.name == name:
                return node
        return None

    def create_child_folder(
        self, *, parent_id: str, name: str, db_path: str, author: str
    ) -> Node:
        """Create a folder node under ``parent_id`` (e.g. the 📝 Tasks folder)."""
        return self._create_node(
            parent_id=parent_id,
            name=name,
            node_type="folder",
            description="",
            db_path=db_path,
            author=author,
        )

    def search_tasks(
        self,
        *,
        hashtag: str,
        db_path: str,
        extra_query: str | None = None,
    ) -> list[Node]:
        """Server-side search for tasks matching ``hashtag`` (and optional free text).

        Wraps ``GET /api/nodes/advanced_search`` with ``node_type=task`` plus
        the supplied filters. Returns flat matches.
        """
        params: dict[str, Any] = {
            "type": "task",
            "hashtag": hashtag,
            "include_linked_dbs": "false",
            "_db_path": db_path,
        }
        if extra_query:
            params["q"] = extra_query
        data = self._request("GET", "/api/nodes/advanced_search", params=params)
        items = data.get("results", data.get("tree", []))
        return [Node.from_api(raw) for raw in items]

    def create_task(
        self,
        *,
        parent_id: str,
        name: str,
        description: str,
        db_path: str,
        author: str,
        status: str = "TODO",
        hashtags: list[str] | None = None,
    ) -> Node:
        """Create a kanban-style task node under ``parent_id``."""
        node = self._create_node(
            parent_id=parent_id,
            name=name,
            node_type="task",
            description=description,
            db_path=db_path,
            author=author,
            status=status,
        )
        if hashtags:
            # Non-fatal: the task exists and is findable by name whether or
            # not the tags land. Aborting here used to strand a freshly
            # created ticket with no tags, no collaborators, and no comment
            # back on the GitHub issue.
            try:
                self.set_hashtags(
                    node_id=node.id, hashtags=hashtags, db_path=db_path
                )
                node.hashtags = list(hashtags)
            except AthenaError as exc:
                print(f"::warning::could not tag {name!r}: {exc}")
        return node

    def create_child_note(
        self,
        *,
        parent_id: str,
        name: str,
        description: str,
        db_path: str,
        author: str,
        node_type: str = "description",
    ) -> Node:
        """Create a note child under a task — the discussion primitive.

        Athena has no comment node type, so GitHub comments become plain
        ``description`` children hanging off the task, and PR links become
        ``solution`` children (a PR is the proposed answer to the ticket).
        """
        return self._create_node(
            parent_id=parent_id,
            name=name,
            node_type=node_type,
            description=description,
            db_path=db_path,
            author=author,
        )

    def set_hashtags(
        self, *, node_id: str, hashtags: list[str], db_path: str
    ) -> None:
        """PUT /api/nodes/{id}/hashtags — used right after create_task."""
        url = f"/api/nodes/{node_id}/hashtags"
        params = {"_db_path": db_path}
        body = {"hashtags": hashtags}
        self._request("PUT", url, params=params, json_body=body)

    def update_node(
        self,
        *,
        node_id: str,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        db_path: str,
    ) -> Node:
        """PUT /api/nodes/{id} — partial update of a node.

        Only fields explicitly provided are sent (``None`` means "do not
        change"). The server returns the updated ``Node``.
        """
        params = {"_db_path": db_path}
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if status is not None:
            body["status"] = status
        if not body:
            raise ValueError(
                "update_node requires at least one of name/description/status"
            )
        data = self._request(
            "PUT", f"/api/nodes/{node_id}", params=params, json_body=body
        )
        return Node.from_api(data.get("node", data))

    def get_collaborators(self, *, node_id: str, db_path: str) -> Collaborators:
        """GET /api/nodes/{id}/collaborators — current lists plus the author."""
        data = self._request(
            "GET",
            f"/api/nodes/{node_id}/collaborators",
            params={"_db_path": db_path},
        )
        return Collaborators.from_api(data)

    def set_collaborators(
        self,
        *,
        node_id: str,
        editors: list[str],
        co_authors: list[str],
        db_path: str,
        apply_to_descendants: bool = False,
    ) -> Collaborators:
        """PUT /api/nodes/{id}/collaborators — replace editor + co-author lists.

        All three body fields are required by the server; omitting
        ``apply_to_descendants`` gets the request rejected outright.
        The server also rejects the node's own author appearing in either
        list, and rejects unknown users.
        """
        data = self._request(
            "PUT",
            f"/api/nodes/{node_id}/collaborators",
            params={"_db_path": db_path},
            json_body={
                "editors": editors,
                "co_authors": co_authors,
                "apply_to_descendants": apply_to_descendants,
            },
        )
        return Collaborators.from_api(data)

    def get_node(self, *, node_id: str, db_path: str) -> Node:
        """GET /api/nodes/external — fetch a single node by id."""
        data = self._request(
            "GET",
            "/api/nodes/external",
            params={"node_id": node_id, "depth": 0, "_db_path": db_path},
        )
        return Node.from_api(data.get("node", data))

    def create_shortlink(self, *, node_id: str, db_path: str) -> str | None:
        """Return the shareable weblink URL for a node, or None on failure.

        ``POST /api/nodes/{id}/shortlink?_db_path=...`` returns
        ``{"shortlink": "[~>42]", "web_token": "kPnWQdxk", ...}``. The
        weblink URL is ``{base_url}/w/{web_token}``.
        """
        url = f"/api/nodes/{node_id}/shortlink"
        try:
            data = self._request("POST", url, params={"_db_path": db_path})
        except AthenaError:
            return None
        web_token = data.get("web_token")
        if not web_token:
            return None
        return f"{self._base_url}/w/{web_token}"

    def _create_node(
        self,
        *,
        parent_id: str,
        name: str,
        node_type: str,
        description: str,
        db_path: str,
        author: str,
        status: str = "",
    ) -> Node:
        payload: dict[str, Any] = {
            "name": name,
            "type": node_type,
            "description": description,
            "status": status,
            "color": "",
            "target_person": "",
            "whitelist": "",
            "parent_node_id": parent_id,
            "_db_path": db_path,
        }
        # An empty author means "whoever this token is". The server stores
        # the field verbatim and then checks *the caller* against it on
        # every later write, so any invented string — a bot's display name,
        # a token name, an email — permanently locks the action out of the
        # node it just created. Only send one if explicitly overridden.
        if author:
            payload["author"] = author
        data = self._request("POST", "/api/nodes", json_body=payload)
        return Node.from_api(data.get("node", data))


def url_encode_db_path(db_path: str) -> str:
    """Match the URL-encoding the server expects in ``_db_path`` query params.

    Not strictly needed for httpx (it handles encoding for query strings), but
    kept available for callers that build URLs by hand.
    """
    return urllib.parse.quote(db_path, safe="")