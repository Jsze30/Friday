from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

CONTEXT_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "Friday" / "context.sqlite3"
)

_FILE_REFERENCES = re.compile(
    r"\b(?:this|that|the|current)\s+(?:file|document)\b", re.IGNORECASE
)
_PAGE_REFERENCES = re.compile(
    r"\b(?:this|that|the|current)\s+(?:page|website|site|tab)\b", re.IGNORECASE
)
_APP_REFERENCES = re.compile(
    r"\b(?:this|that|the|current)\s+app(?:lication)?\b", re.IGNORECASE
)
_PROJECT_REFERENCES = re.compile(
    r"\b(?:this|that|the|current)\s+(?:project|repo|repository)\b",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean_text(value: Any, *, maximum: int) -> str:
    text = " ".join(str(value or "").split()).strip(" \t\r\n\"'")
    if not text:
        raise ValueError("value cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"value cannot exceed {maximum} characters")
    return text


def _path_from_document(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urlsplit(text)
        return Path(unquote(parsed.path)).expanduser()
    if text.startswith("/"):
        return Path(text).expanduser()
    return None


def _project_for_document(document: Any) -> dict[str, str] | None:
    path = _path_from_document(document)
    if path is None:
        return None
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            return {"name": parent.name, "path": str(parent)}
    return None


class ContextStore:
    def __init__(self, path: Path = CONTEXT_DB_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._latest_working: dict[str, Any] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_memories (
                    alias TEXT PRIMARY KEY COLLATE NOCASE,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'entity',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def remember_reference(
        self,
        alias: str,
        target: str,
        *,
        kind: str = "entity",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cleaned_alias = _clean_text(alias, maximum=120)
        display_target = _clean_text(target, maximum=500)
        cleaned_target = display_target
        cleaned_kind = _clean_text(kind or "entity", maximum=40).casefold()
        resolved_metadata = dict(metadata or {})
        with self._lock:
            latest_working = dict(self._latest_working)
        project = latest_working.get("project")
        if isinstance(project, dict):
            project_name = str(project.get("name") or "")
            project_path = str(project.get("path") or "")
            refers_to_project = "project" in cleaned_alias.casefold()
            target_matches_project = (
                project_name and display_target.casefold() == project_name.casefold()
            )
            if project_path and (refers_to_project or target_matches_project):
                cleaned_kind = "project"
                cleaned_target = project_path
                resolved_metadata.setdefault("label", display_target)
        timestamp = _now()
        encoded_metadata = json.dumps(resolved_metadata, sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reference_memories (
                    alias, target, kind, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET
                    target = excluded.target,
                    kind = excluded.kind,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cleaned_alias,
                    cleaned_target,
                    cleaned_kind,
                    encoded_metadata,
                    timestamp,
                    timestamp,
                ),
            )
        return {
            "alias": cleaned_alias,
            "target": cleaned_target,
            "kind": cleaned_kind,
            "metadata": resolved_metadata,
            "updatedAt": timestamp,
        }

    def forget_reference(self, alias: str) -> bool:
        cleaned_alias = _clean_text(alias, maximum=120)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM reference_memories WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            )
        return cursor.rowcount > 0

    def list_references(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT alias, target, kind, metadata_json, created_at,
                       updated_at, use_count
                FROM reference_memories
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [self._row_to_reference(row) for row in rows]

    def resolve(
        self,
        query: str,
        working: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_query = " ".join((query or "").split())
        snapshot = dict(working or {})
        project = snapshot.get("project")
        if not isinstance(project, dict):
            project = _project_for_document(snapshot.get("currentDocument"))
            if project:
                snapshot["project"] = project
        with self._lock:
            self._latest_working = dict(snapshot)

        resolutions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        matched_aliases: list[str] = []

        for memory in sorted(
            self.list_references(limit=500),
            key=lambda item: len(str(item["alias"])),
            reverse=True,
        ):
            alias = str(memory["alias"])
            if not re.search(
                rf"(?<!\w){re.escape(alias)}(?!\w)",
                normalized_query,
                re.IGNORECASE,
            ):
                continue
            self._append_resolution(
                resolutions,
                seen,
                phrase=alias,
                target=str(memory["target"]),
                kind=str(memory["kind"]),
                source="saved memory",
                confidence=1.0,
            )
            matched_aliases.append(alias)

        current_document = snapshot.get("currentDocument")
        file_match = _FILE_REFERENCES.search(normalized_query)
        if (
            current_document
            and file_match
            and file_match.group(0).casefold()
            not in {item["phrase"].casefold() for item in resolutions}
        ):
            path = _path_from_document(current_document)
            target = str(path) if path else str(current_document)
            self._append_resolution(
                resolutions,
                seen,
                phrase=file_match.group(0),
                target=target,
                kind="file",
                source="active document",
                confidence=0.98,
            )

        current_url = snapshot.get("currentURL")
        page_match = _PAGE_REFERENCES.search(normalized_query)
        if (
            current_url
            and page_match
            and page_match.group(0).casefold()
            not in {item["phrase"].casefold() for item in resolutions}
        ):
            self._append_resolution(
                resolutions,
                seen,
                phrase=page_match.group(0),
                target=str(current_url),
                kind="webpage",
                source="active browser page",
                confidence=0.96,
            )

        current_app = snapshot.get("currentApplication")
        if isinstance(current_app, dict):
            current_app = current_app.get("name")
        app_match = _APP_REFERENCES.search(normalized_query)
        if (
            current_app
            and app_match
            and app_match.group(0).casefold()
            not in {item["phrase"].casefold() for item in resolutions}
        ):
            self._append_resolution(
                resolutions,
                seen,
                phrase=app_match.group(0),
                target=str(current_app),
                kind="app",
                source="frontmost application",
                confidence=0.99,
            )

        project_match = _PROJECT_REFERENCES.search(normalized_query)
        if (
            project
            and project_match
            and project_match.group(0).casefold()
            not in {item["phrase"].casefold() for item in resolutions}
        ):
            target = str(project.get("path") or project.get("name") or "")
            if target:
                self._append_resolution(
                    resolutions,
                    seen,
                    phrase=project_match.group(0),
                    target=target,
                    kind="project",
                    source="active project",
                    confidence=0.94,
                )

        if matched_aliases:
            with self._lock, self._connect() as connection:
                connection.executemany(
                    """
                    UPDATE reference_memories
                    SET use_count = use_count + 1
                    WHERE alias = ? COLLATE NOCASE
                    """,
                    ((alias,) for alias in matched_aliases),
                )

        return {
            "workingContext": snapshot,
            "resolutions": resolutions,
            "retrievedAt": _now(),
        }

    @staticmethod
    def _append_resolution(
        resolutions: list[dict[str, Any]],
        seen: set[tuple[str, str]],
        *,
        phrase: str,
        target: str,
        kind: str,
        source: str,
        confidence: float,
    ) -> None:
        key = (phrase.casefold(), target.casefold())
        if key in seen:
            return
        seen.add(key)
        resolutions.append(
            {
                "phrase": phrase,
                "target": target,
                "kind": kind,
                "source": source,
                "confidence": confidence,
            }
        )

    @staticmethod
    def _row_to_reference(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return {
            "alias": row["alias"],
            "target": row["target"],
            "kind": row["kind"],
            "metadata": metadata,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "useCount": row["use_count"],
        }


store = ContextStore()
