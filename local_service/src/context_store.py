from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

CONTEXT_DB_PATH = (
    Path.home() / "Library" / "Application Support" / "Friday" / "context.sqlite3"
)

SCHEMA_VERSION = 2
MAX_CONTEXT_CHARACTERS = 8_000
MAX_WORKING_CONTEXT_CHARACTERS = 3_000
MAX_RESOLUTIONS = 6
MAX_RETRIEVED_MEMORIES = 8
MAX_TIMELINE_EVENTS = 6
MAX_PREFERENCES = 8
MAX_STORED_EVENTS = 10_000
LOW_VALUE_EVENT_DAYS = 30
DEFAULT_EVENT_DAYS = 180
CONVERSATION_COMPACTION_DAYS = 7
DELETED_RETENTION_DAYS = 30

_FILE_REFERENCES = re.compile(
    r"\b(?:this|that|the|current|selected|open)\s+(?:file|document)\b",
    re.IGNORECASE,
)
_PAGE_REFERENCES = re.compile(
    r"\b(?:this|that|the|current|open)\s+(?:page|website|site|tab)\b",
    re.IGNORECASE,
)
_APP_REFERENCES = re.compile(
    r"\b(?:this|that|the|current|frontmost|open)\s+app(?:lication)?\b",
    re.IGNORECASE,
)
_PROJECT_REFERENCES = re.compile(
    r"\b(?:this|that|the|current|open)\s+(?:project|repo|repository)\b",
    re.IGNORECASE,
)
_PERSON_REFERENCES = re.compile(
    r"\b(?:him|her|them|that person|this person|they|he|she)\b",
    re.IGNORECASE,
)
_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "do",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "who",
    "with",
    "you",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _future(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _clean_text(value: Any, *, maximum: int) -> str:
    text = " ".join(str(value or "").split()).strip(" \t\r\n\"'")
    if not text:
        raise ValueError("value cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"value cannot exceed {maximum} characters")
    return text


def _canonical(value: str) -> str:
    return " ".join(value.casefold().split())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _fingerprint(*values: Any) -> str:
    encoded = _json(values).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _bounded_value(value: Any, *, maximum: int) -> Any:
    """Return JSON-safe context without allowing one source to dominate a turn."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= maximum else value[: maximum - 1] + "…"
    if isinstance(value, list | tuple):
        return [_bounded_value(item, maximum=max(80, maximum // 4)) for item in value[:12]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:30]:
            if str(key).casefold() in {"password", "token", "secret", "audio", "imagebase64"}:
                continue
            result[str(key)[:80]] = _bounded_value(
                item,
                maximum=max(80, maximum // 3),
            )
        return result
    return str(value)[:maximum]


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[\w'-]{2,}", query.casefold().replace("_", " "))
    return [term for term in terms if term not in _STOP_WORDS][:12]


class ContextStore:
    """Local durable context, timeline, graph, preferences, and retrieval service."""

    def __init__(self, path: Path = CONTEXT_DB_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._latest_working: dict[str, Any] = {}
        self._latest_working_signature = ""
        self._writes_since_retention = 0
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    attributes_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS entities_active_key
                    ON entities(kind, canonical_key)
                    WHERE deleted_at IS NULL;
                CREATE INDEX IF NOT EXISTS entities_kind_updated
                    ON entities(kind, updated_at DESC);

                CREATE TABLE IF NOT EXISTS entity_aliases (
                    alias TEXT NOT NULL COLLATE NOCASE,
                    scope TEXT NOT NULL DEFAULT 'general',
                    entity_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(alias, scope),
                    FOREIGN KEY(entity_id) REFERENCES entities(id),
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );

                CREATE TABLE IF NOT EXISTS relationships (
                    id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_id TEXT,
                    object_value_json TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY(subject_id) REFERENCES entities(id),
                    FOREIGN KEY(object_id) REFERENCES entities(id),
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE INDEX IF NOT EXISTS relationships_subject
                    ON relationships(subject_id, predicate, updated_at DESC);
                CREATE INDEX IF NOT EXISTS relationships_object
                    ON relationships(object_id, predicate, updated_at DESC);

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.5,
                    expires_at TEXT,
                    session_id TEXT,
                    turn_id TEXT,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE INDEX IF NOT EXISTS events_time
                    ON events(occurred_at DESC);
                CREATE INDEX IF NOT EXISTS events_session
                    ON events(session_id, occurred_at DESC);

                CREATE TABLE IF NOT EXISTS event_entities (
                    event_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'related',
                    PRIMARY KEY(event_id, entity_id, role),
                    FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
                    FOREIGN KEY(entity_id) REFERENCES entities(id)
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY COLLATE NOCASE,
                    value_json TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );

                CREATE TABLE IF NOT EXISTS mentions (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'entity',
                    session_id TEXT,
                    turn_id TEXT,
                    mentioned_at TEXT NOT NULL,
                    FOREIGN KEY(entity_id) REFERENCES entities(id)
                );
                CREATE TABLE IF NOT EXISTS corrections (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    target_id TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    source_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );

                CREATE TABLE IF NOT EXISTS reference_memories (
                    alias TEXT PRIMARY KEY COLLATE NOCASE,
                    target TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'entity',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS context_search USING fts5(
                    memory_id UNINDEXED,
                    memory_kind UNINDEXED,
                    content,
                    tokenize='unicode61 remove_diacritics 2'
                );
                """
            )
            # An early development schema accidentally lacked this discriminator.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(mentions)").fetchall()
            }
            if "kind" not in columns:
                connection.execute(
                    "ALTER TABLE mentions ADD COLUMN kind TEXT NOT NULL DEFAULT 'entity'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS mentions_recent ON mentions(kind, mentioned_at DESC)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO context_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._ensure_source(connection, "system", "system", "Friday context engine")
            self._ensure_source(connection, "user", "user", "Explicit user statement")
            self._ensure_source(connection, "profile", "profile", "Friday local profile")
            self._ensure_source(connection, "mac", "device", "Current Mac activity")
            self._ensure_source(connection, "agent", "conversation", "Friday conversation")
        self.run_retention()

    @staticmethod
    def _ensure_source(
        connection: sqlite3.Connection,
        source_id: str,
        kind: str,
        label: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO sources(id, kind, label, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind = excluded.kind,
                label = excluded.label,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (source_id, kind, label, _json(metadata or {}), timestamp, timestamp),
        )
        return source_id

    def register_source(
        self,
        source_id: str,
        *,
        kind: str,
        label: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        cleaned_id = _clean_text(source_id, maximum=100)
        with self._lock, self._connect() as connection:
            return self._ensure_source(
                connection,
                cleaned_id,
                _clean_text(kind, maximum=60),
                _clean_text(label, maximum=200),
                metadata,
            )

    def upsert_entity(
        self,
        kind: str,
        name: str,
        *,
        canonical_key: str | None = None,
        attributes: dict[str, Any] | None = None,
        source: str = "user",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        cleaned_kind = _clean_text(kind, maximum=60).casefold()
        cleaned_name = _clean_text(name, maximum=500)
        key = _canonical(canonical_key or cleaned_name)
        timestamp = _now()
        with self._lock, self._connect() as connection:
            self._ensure_source(connection, source, source, source)
            row = connection.execute(
                """
                SELECT id, attributes_json FROM entities
                WHERE kind = ? AND canonical_key = ? AND deleted_at IS NULL
                """,
                (cleaned_kind, key),
            ).fetchone()
            entity_id = row["id"] if row else f"entity:{cleaned_kind}:{uuid.uuid4().hex}"
            existing = _decode_json(row["attributes_json"], {}) if row else {}
            merged = {**existing, **(attributes or {})}
            connection.execute(
                """
                INSERT INTO entities(
                    id, kind, name, canonical_key, attributes_json, confidence,
                    source_id, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    attributes_json = excluded.attributes_json,
                    confidence = MAX(entities.confidence, excluded.confidence),
                    source_id = excluded.source_id,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (
                    entity_id,
                    cleaned_kind,
                    cleaned_name,
                    key,
                    _json(_bounded_value(merged, maximum=4_000)),
                    min(1.0, max(0.0, confidence)),
                    source,
                    timestamp,
                    timestamp,
                ),
            )
            self._index_memory(
                connection,
                entity_id,
                "entity",
                f"{cleaned_name} {cleaned_kind} {_json(merged)}",
            )
            result = self._entity_by_id(connection, entity_id)
        self._after_write()
        assert result is not None
        return result

    def add_relationship(
        self,
        subject_id: str,
        predicate: str,
        *,
        object_id: str | None = None,
        object_value: Any = None,
        source: str = "user",
        confidence: float = 1.0,
        observed_at: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cleaned_predicate = _clean_text(predicate, maximum=100).casefold().replace(" ", "_")
        if object_id is None and object_value is None:
            raise ValueError("a relationship requires object_id or object_value")
        timestamp = _now()
        encoded_value = _json(object_value) if object_id is None else None
        with self._lock, self._connect() as connection:
            self._ensure_source(connection, source, source, source)
            existing = connection.execute(
                """
                SELECT id FROM relationships
                WHERE subject_id = ? AND predicate = ?
                  AND COALESCE(object_id, '') = COALESCE(?, '')
                  AND COALESCE(object_value_json, '') = COALESCE(?, '')
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (subject_id, cleaned_predicate, object_id, encoded_value),
            ).fetchone()
            relationship_id = (
                existing["id"] if existing else f"relationship:{uuid.uuid4().hex}"
            )
            connection.execute(
                """
                INSERT INTO relationships(
                    id, subject_id, predicate, object_id, object_value_json,
                    confidence, source_id, observed_at, valid_from, valid_until,
                    metadata_json, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    confidence = excluded.confidence,
                    source_id = excluded.source_id,
                    observed_at = excluded.observed_at,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (
                    relationship_id,
                    subject_id,
                    cleaned_predicate,
                    object_id,
                    encoded_value,
                    min(1.0, max(0.0, confidence)),
                    source,
                    observed_at or timestamp,
                    valid_from,
                    valid_until,
                    _json(metadata or {}),
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT r.*, s.name AS subject_name, o.name AS object_name
                FROM relationships r
                JOIN entities s ON s.id = r.subject_id
                LEFT JOIN entities o ON o.id = r.object_id
                WHERE r.id = ?
                """,
                (relationship_id,),
            ).fetchone()
            assert row is not None
            content = (
                f"{row['subject_name']} {cleaned_predicate} "
                f"{row['object_name'] or encoded_value or ''}"
            )
            self._index_memory(connection, relationship_id, "relationship", content)
            result = self._row_to_relationship(row)
        self._after_write()
        return result

    def remember_fact(
        self,
        subject: str,
        predicate: str,
        object_value: str,
        *,
        subject_kind: str = "entity",
        object_kind: str = "entity",
        source: str = "user",
    ) -> dict[str, Any]:
        subject_entity = self.upsert_entity(subject_kind, subject, source=source)
        object_entity = self.upsert_entity(object_kind, object_value, source=source)
        relationship = self.add_relationship(
            subject_entity["id"],
            predicate,
            object_id=object_entity["id"],
            source=source,
            confidence=1.0,
        )
        self._record_correction("remember_fact", relationship["id"], None, relationship)
        return relationship

    def set_preference(
        self,
        key: str,
        value: Any,
        *,
        source: str = "user",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        cleaned_key = _clean_text(key, maximum=120).casefold().replace(" ", "_")
        timestamp = _now()
        with self._lock, self._connect() as connection:
            self._ensure_source(connection, source, source, source)
            before = connection.execute(
                "SELECT * FROM preferences WHERE key = ? COLLATE NOCASE",
                (cleaned_key,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO preferences(
                    key, value_json, confidence, source_id, created_at, updated_at,
                    deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    confidence = excluded.confidence,
                    source_id = excluded.source_id,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL
                """,
                (
                    cleaned_key,
                    _json(_bounded_value(value, maximum=2_000)),
                    min(1.0, max(0.0, confidence)),
                    source,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM preferences WHERE key = ? COLLATE NOCASE",
                (cleaned_key,),
            ).fetchone()
            assert row is not None
            result = self._row_to_preference(row)
            self._index_memory(
                connection,
                f"preference:{cleaned_key}",
                "preference",
                f"{cleaned_key} {_json(value)}",
            )
        if source == "user":
            self._record_correction(
                "set_preference",
                f"preference:{cleaned_key}",
                self._row_to_preference(before) if before else None,
                result,
            )
        self._after_write()
        return result

    def import_profile(self, profile: dict[str, Any]) -> int:
        facts = profile.get("facts") or {}
        if not isinstance(facts, dict):
            return 0
        for key, value in facts.items():
            self.set_preference(
                str(key),
                value,
                source="profile",
                confidence=1.0,
            )
        return len(facts)

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
        cleaned_kind = _clean_text(kind or "entity", maximum=60).casefold()
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

        entity = self.upsert_entity(
            cleaned_kind,
            resolved_metadata.get("label") or display_target,
            canonical_key=cleaned_target,
            attributes={
                **resolved_metadata,
                "target": cleaned_target,
            },
            source="user",
        )
        timestamp = _now()
        with self._lock, self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM reference_memories WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO reference_memories(
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
                    _json(resolved_metadata),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO entity_aliases(
                    alias, scope, entity_id, confidence, source_id, created_at, updated_at
                ) VALUES (?, 'general', ?, 1.0, 'user', ?, ?)
                ON CONFLICT(alias, scope) DO UPDATE SET
                    entity_id = excluded.entity_id,
                    confidence = excluded.confidence,
                    source_id = excluded.source_id,
                    updated_at = excluded.updated_at
                """,
                (cleaned_alias, entity["id"], timestamp, timestamp),
            )
            self._reindex_entity(connection, entity["id"])
        result = {
            "id": f"reference:{cleaned_alias.casefold()}",
            "alias": cleaned_alias,
            "target": cleaned_target,
            "kind": cleaned_kind,
            "metadata": resolved_metadata,
            "entityId": entity["id"],
            "source": "user",
            "confidence": 1.0,
            "updatedAt": timestamp,
        }
        self._record_correction(
            "remember_reference",
            result["id"],
            self._row_to_reference(previous) if previous else None,
            result,
        )
        self._after_write()
        return result

    def forget_reference(self, alias: str) -> bool:
        cleaned_alias = _clean_text(alias, maximum=120)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reference_memories WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            ).fetchone()
            alias_row = connection.execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            ).fetchone()
            cursor = connection.execute(
                "DELETE FROM reference_memories WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            )
            connection.execute(
                "DELETE FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
                (cleaned_alias,),
            )
            if alias_row:
                entity_id = alias_row["entity_id"]
                still_referenced = connection.execute(
                    """
                    SELECT
                        EXISTS(SELECT 1 FROM entity_aliases WHERE entity_id = ?) OR
                        EXISTS(SELECT 1 FROM relationships
                               WHERE (subject_id = ? OR object_id = ?)
                                 AND deleted_at IS NULL) OR
                        EXISTS(SELECT 1 FROM event_entities WHERE entity_id = ?) OR
                        EXISTS(SELECT 1 FROM mentions WHERE entity_id = ?)
                    """,
                    (entity_id, entity_id, entity_id, entity_id, entity_id),
                ).fetchone()[0]
                if not still_referenced:
                    timestamp = _now()
                    connection.execute(
                        "UPDATE entities SET deleted_at = ?, updated_at = ? WHERE id = ?",
                        (timestamp, timestamp, entity_id),
                    )
                    connection.execute(
                        "DELETE FROM context_search WHERE memory_id = ?",
                        (entity_id,),
                    )
        if cursor.rowcount:
            self._record_correction(
                "forget_reference",
                f"reference:{cleaned_alias.casefold()}",
                self._row_to_reference(row) if row else None,
                None,
            )
            self._after_write()
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

    def record_event(
        self,
        event_type: str,
        summary: str,
        *,
        data: dict[str, Any] | None = None,
        source: str = "agent",
        confidence: float = 1.0,
        importance: float = 0.5,
        occurred_at: str | None = None,
        expires_at: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        entity_ids: Iterable[str] = (),
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        cleaned_type = _clean_text(event_type, maximum=120).casefold()
        cleaned_summary = _clean_text(summary, maximum=2_000)
        timestamp = occurred_at or _now()
        bounded_data = _bounded_value(data or {}, maximum=5_000)
        event_fingerprint = fingerprint or _fingerprint(
            cleaned_type,
            cleaned_summary,
            session_id,
            turn_id,
        )
        event_id = f"event:{uuid.uuid4().hex}"
        with self._lock, self._connect() as connection:
            self._ensure_source(connection, source, source, source)
            existing = connection.execute(
                "SELECT * FROM events WHERE fingerprint = ?",
                (event_fingerprint,),
            ).fetchone()
            if existing:
                return self._row_to_event(existing)
            connection.execute(
                """
                INSERT INTO events(
                    id, event_type, summary, occurred_at, source_id, confidence,
                    importance, expires_at, session_id, turn_id, data_json,
                    fingerprint, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    event_id,
                    cleaned_type,
                    cleaned_summary,
                    timestamp,
                    source,
                    min(1.0, max(0.0, confidence)),
                    min(1.0, max(0.0, importance)),
                    expires_at,
                    session_id,
                    turn_id,
                    _json(bounded_data),
                    event_fingerprint,
                    _now(),
                ),
            )
            for entity_id in set(entity_ids):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO event_entities(event_id, entity_id, role)
                    VALUES (?, ?, 'related')
                    """,
                    (event_id, entity_id),
                )
            self._index_memory(
                connection,
                event_id,
                "event",
                f"{cleaned_type} {cleaned_summary} {_json(bounded_data)}",
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
        self._record_mentions_from_text(
            cleaned_summary,
            session_id=session_id,
            turn_id=turn_id,
            role="event",
            mentioned_at=timestamp,
        )
        self._after_write()
        assert row is not None
        return self._row_to_event(row)

    def record_client_event(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        event_type = str(payload.get("type") or "").strip()
        turn_id = str(payload.get("turnId") or "") or None
        session_id = str(payload.get("sessionId") or "") or None
        timestamp = str(payload.get("timestamp") or "") or _now()

        if event_type == "transcript":
            if payload.get("isFinal") is not True:
                return None
            role = str(payload.get("role") or "unknown")
            text = " ".join(str(payload.get("text") or "").split())
            if not text:
                return None
            label = "User" if role == "user" else "Friday"
            return self.record_event(
                "conversation.turn",
                f"{label}: {text}",
                data={"role": role, "text": text, "interrupted": payload.get("interrupted")},
                source="agent",
                confidence=1.0,
                importance=0.6 if role == "user" else 0.45,
                occurred_at=timestamp,
                expires_at=_future(DEFAULT_EVENT_DAYS),
                session_id=session_id,
                turn_id=turn_id,
                fingerprint=_fingerprint("transcript", turn_id, role, text),
            )

        if event_type in {"action_completed", "capability_completed"}:
            identifier = str(payload.get("action") or payload.get("capability") or "work")
            ok = bool(payload.get("ok"))
            outcome = "completed" if ok else "failed"
            return self.record_event(
                event_type,
                f"{identifier} {outcome}",
                data=payload,
                source="agent",
                confidence=1.0,
                importance=0.8 if ok else 0.65,
                occurred_at=timestamp,
                expires_at=None if ok else _future(DEFAULT_EVENT_DAYS),
                session_id=session_id,
                turn_id=turn_id,
                fingerprint=_fingerprint(event_type, turn_id, identifier, ok, payload.get("result")),
            )
        return None

    def ingest_working_context(self, working: dict[str, Any] | None) -> dict[str, Any]:
        snapshot = _bounded_value(working or {}, maximum=MAX_WORKING_CONTEXT_CHARACTERS)
        if not isinstance(snapshot, dict):
            snapshot = {}
        project = snapshot.get("project")
        if not isinstance(project, dict):
            project = _project_for_document(snapshot.get("currentDocument"))
            if project:
                snapshot["project"] = project

        entity_ids: list[str] = []
        app = snapshot.get("currentApplication")
        if isinstance(app, dict) and app.get("name"):
            entity = self.upsert_entity(
                "app",
                str(app["name"]),
                canonical_key=str(app.get("bundleId") or app["name"]),
                attributes=app,
                source="mac",
                confidence=0.99,
            )
            entity_ids.append(entity["id"])

        document_path = _path_from_document(snapshot.get("currentDocument"))
        file_entity: dict[str, Any] | None = None
        if document_path:
            file_entity = self.upsert_entity(
                "file",
                document_path.name or str(document_path),
                canonical_key=str(document_path),
                attributes={"path": str(document_path)},
                source="mac",
                confidence=0.99,
            )
            entity_ids.append(file_entity["id"])

        project_entity: dict[str, Any] | None = None
        if isinstance(project, dict) and (project.get("path") or project.get("name")):
            project_name = str(project.get("name") or Path(str(project["path"])).name)
            project_path = str(project.get("path") or project_name)
            project_entity = self.upsert_entity(
                "project",
                project_name,
                canonical_key=project_path,
                attributes={"path": project_path},
                source="mac",
                confidence=0.98,
            )
            entity_ids.append(project_entity["id"])
        if file_entity and project_entity:
            self.add_relationship(
                project_entity["id"],
                "contains_file",
                object_id=file_entity["id"],
                source="mac",
                confidence=0.99,
            )

        current_url = snapshot.get("currentURL")
        if isinstance(current_url, str) and current_url:
            page = self.upsert_entity(
                "webpage",
                str(snapshot.get("currentWindow") or current_url)[:500],
                canonical_key=current_url,
                attributes={"url": current_url},
                source="mac",
                confidence=0.97,
            )
            entity_ids.append(page["id"])

        signature_payload = {
            key: snapshot.get(key)
            for key in (
                "currentApplication",
                "currentWindow",
                "currentDocument",
                "currentURL",
                "project",
            )
            if snapshot.get(key) is not None
        }
        signature = _fingerprint(signature_payload)
        with self._lock:
            previous_signature = self._latest_working_signature
            self._latest_working = dict(snapshot)
            self._latest_working_signature = signature
        if signature_payload and signature != previous_signature:
            description = self._working_summary(snapshot)
            self.record_event(
                "context.activity_changed",
                description,
                data=signature_payload,
                source="mac",
                confidence=0.99,
                importance=0.25,
                expires_at=_future(LOW_VALUE_EVENT_DAYS),
                entity_ids=entity_ids,
                fingerprint=_fingerprint(
                    signature,
                    datetime.now(UTC).strftime("%Y-%m-%dT%H:%M"),
                ),
            )
        return snapshot

    def resolve(
        self,
        query: str,
        working: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        max_characters: int = MAX_CONTEXT_CHARACTERS,
    ) -> dict[str, Any]:
        normalized_query = " ".join((query or "").split())[:2_000]
        snapshot = self.ingest_working_context(working)
        project = snapshot.get("project")
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
        if current_document and file_match and not self._phrase_is_resolved(
            resolutions, file_match.group(0)
        ):
            path = _path_from_document(current_document)
            self._append_resolution(
                resolutions,
                seen,
                phrase=file_match.group(0),
                target=str(path) if path else str(current_document),
                kind="file",
                source="active document",
                confidence=0.98,
            )

        current_url = snapshot.get("currentURL")
        page_match = _PAGE_REFERENCES.search(normalized_query)
        if current_url and page_match and not self._phrase_is_resolved(
            resolutions, page_match.group(0)
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
        if current_app and app_match and not self._phrase_is_resolved(
            resolutions, app_match.group(0)
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
            isinstance(project, dict)
            and project_match
            and not self._phrase_is_resolved(resolutions, project_match.group(0))
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

        person_match = _PERSON_REFERENCES.search(normalized_query)
        if person_match:
            recent_person = self._recent_mention("person", session_id=session_id)
            if recent_person:
                self._append_resolution(
                    resolutions,
                    seen,
                    phrase=person_match.group(0),
                    target=recent_person["name"],
                    kind="person",
                    source="recent conversation",
                    confidence=0.9 if recent_person.get("sessionMatch") else 0.76,
                    memory_id=recent_person["id"],
                )

        if matched_aliases:
            with self._lock, self._connect() as connection:
                connection.executemany(
                    """
                    UPDATE reference_memories SET use_count = use_count + 1
                    WHERE alias = ? COLLATE NOCASE
                    """,
                    ((alias,) for alias in matched_aliases),
                )

        memories = self.search_memories(normalized_query, limit=MAX_RETRIEVED_MEMORIES)
        timeline = self.search_timeline(
            normalized_query,
            limit=MAX_TIMELINE_EVENTS,
            session_id=session_id,
        )
        preferences = self.relevant_preferences(
            normalized_query,
            limit=MAX_PREFERENCES,
        )
        result: dict[str, Any] = {
            "workingContext": snapshot,
            "resolutions": resolutions[:MAX_RESOLUTIONS],
            "retrievedMemories": memories,
            "timeline": timeline,
            "preferences": preferences,
            "retrievedAt": _now(),
        }
        return self._bound_context_result(result, max_characters=max_characters)

    def search_memories(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        terms = _query_terms(query)
        if not terms:
            return []
        match_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
        bounded_limit = max(1, min(limit, 25))
        with self._lock, self._connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT memory_id, memory_kind, bm25(context_search) AS rank
                    FROM context_search
                    WHERE context_search MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match_query, bounded_limit * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            results: list[dict[str, Any]] = []
            for row in rows:
                memory = self._memory_by_id(connection, row["memory_id"])
                if memory is None or memory.get("type") == "event":
                    continue
                memory["relevance"] = round(max(0.0, 1.0 / (1.0 + abs(row["rank"]))), 4)
                results.append(memory)
                if len(results) >= bounded_limit:
                    break
        return results

    def search_timeline(
        self,
        query: str = "",
        *,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 100))
        terms = _query_terms(query)
        with self._lock, self._connect() as connection:
            if terms:
                match_query = " OR ".join(
                    f'"{term.replace(chr(34), "")}"' for term in terms
                )
                try:
                    rows = connection.execute(
                        """
                        SELECT e.*
                        FROM context_search s
                        JOIN events e ON e.id = s.memory_id
                        WHERE context_search MATCH ? AND e.deleted_at IS NULL
                          AND (e.expires_at IS NULL OR e.expires_at > ?)
                        ORDER BY
                          CASE WHEN e.session_id = ? THEN 0 ELSE 1 END,
                          e.importance DESC,
                          e.occurred_at DESC
                        LIMIT ?
                        """,
                        (match_query, _now(), session_id, bounded_limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE deleted_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY occurred_at DESC
                    LIMIT ?
                    """,
                    (_now(), bounded_limit),
                ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def relevant_preferences(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        terms = set(_query_terms(query))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM preferences
                WHERE deleted_at IS NULL
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 100
                """
            ).fetchall()
        preferences = [self._row_to_preference(row) for row in rows]
        if not terms:
            return preferences[:limit]
        matched = []
        for item in preferences:
            item_terms = set(_query_terms(f"{item['key']} {_json(item['value'])}"))
            if any(
                query_term == item_term
                or query_term.startswith(item_term)
                or item_term.startswith(query_term)
                for query_term in terms
                for item_term in item_terms
            ):
                matched.append(item)
        return (matched or preferences)[:limit]

    def list_memories(
        self,
        *,
        kind: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        memories: list[dict[str, Any]] = []
        normalized_kind = (kind or "").casefold()
        if normalized_kind in {"", "reference"}:
            memories.extend(
                {
                    **item,
                    "type": "reference",
                    "title": item["alias"],
                    "detail": item["metadata"].get("label") or item["target"],
                    "source": "user",
                    "confidence": 1.0,
                }
                for item in self.list_references(limit=bounded_limit)
            )
        with self._lock, self._connect() as connection:
            if normalized_kind in {"", "preference"}:
                rows = connection.execute(
                    """
                    SELECT * FROM preferences WHERE deleted_at IS NULL
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
                memories.extend(
                    {
                        **self._row_to_preference(row),
                        "type": "preference",
                        "title": row["key"],
                        "detail": str(_decode_json(row["value_json"], None)),
                    }
                    for row in rows
                )
            if normalized_kind in {"", "entity"}:
                rows = connection.execute(
                    """
                    SELECT * FROM entities WHERE deleted_at IS NULL
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
                memories.extend(
                    {
                        **self._row_to_entity(row),
                        "type": "entity",
                        "title": row["name"],
                        "detail": row["kind"],
                    }
                    for row in rows
                )
            if normalized_kind in {"", "relationship"}:
                rows = connection.execute(
                    """
                    SELECT r.*, s.name AS subject_name, o.name AS object_name
                    FROM relationships r
                    JOIN entities s ON s.id = r.subject_id
                    LEFT JOIN entities o ON o.id = r.object_id
                    WHERE r.deleted_at IS NULL
                    ORDER BY r.updated_at DESC LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
                memories.extend(
                    {
                        **self._row_to_relationship(row),
                        "type": "relationship",
                        "title": f"{row['subject_name']} {row['predicate']}",
                        "detail": str(row["object_name"] or _decode_json(row["object_value_json"], None)),
                    }
                    for row in rows
                )
            if normalized_kind in {"event", "timeline"}:
                rows = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE deleted_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY occurred_at DESC LIMIT ?
                    """,
                    (_now(), bounded_limit),
                ).fetchall()
                memories.extend(
                    {
                        **self._row_to_event(row),
                        "type": "event",
                        "title": row["summary"],
                        "detail": row["event_type"],
                        "updatedAt": row["occurred_at"],
                    }
                    for row in rows
                )
        if query:
            terms = _query_terms(query)
            memories = [
                item
                for item in memories
                if all(
                    term in _canonical(f"{item.get('title', '')} {item.get('detail', '')}")
                    for term in terms
                )
            ]
        memories.sort(key=lambda item: str(item.get("updatedAt") or item.get("createdAt") or ""), reverse=True)
        return memories[:bounded_limit]

    def forget_memory(self, memory_id: str) -> bool:
        cleaned = _clean_text(memory_id, maximum=300)
        if cleaned.startswith("reference:"):
            references = self.list_references(limit=500)
            target = next(
                (
                    item["alias"]
                    for item in references
                    if f"reference:{item['alias'].casefold()}" == cleaned.casefold()
                ),
                None,
            )
            return self.forget_reference(target) if target else False
        timestamp = _now()
        changed = 0
        with self._lock, self._connect() as connection:
            if cleaned.startswith("preference:"):
                key = cleaned.removeprefix("preference:")
                cursor = connection.execute(
                    "UPDATE preferences SET deleted_at = ?, updated_at = ? WHERE key = ? AND deleted_at IS NULL",
                    (timestamp, timestamp, key),
                )
            elif cleaned.startswith("entity:"):
                cursor = connection.execute(
                    "UPDATE entities SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (timestamp, timestamp, cleaned),
                )
                connection.execute(
                    "UPDATE relationships SET deleted_at = ?, updated_at = ? WHERE (subject_id = ? OR object_id = ?) AND deleted_at IS NULL",
                    (timestamp, timestamp, cleaned, cleaned),
                )
                connection.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (cleaned,))
            elif cleaned.startswith("relationship:"):
                cursor = connection.execute(
                    "UPDATE relationships SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (timestamp, timestamp, cleaned),
                )
            elif cleaned.startswith("event:"):
                cursor = connection.execute(
                    "UPDATE events SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (timestamp, cleaned),
                )
            else:
                return False
            changed = cursor.rowcount
            if changed:
                connection.execute("DELETE FROM context_search WHERE memory_id = ?", (cleaned,))
        if changed:
            self._record_correction("forget_memory", cleaned, {"id": cleaned}, None)
            self._after_write()
        return changed > 0

    def forget_latest_explicit_memory(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT target_id FROM corrections
                WHERE operation IN ('remember_reference', 'remember_fact', 'set_preference')
                  AND target_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        if not row or not self.forget_memory(row["target_id"]):
            return None
        return {"id": row["target_id"], "forgotten": True}

    def status(self) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            counts = {}
            for table in ("entities", "relationships", "events", "preferences"):
                counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NULL"
                ).fetchone()[0]
            counts["references"] = connection.execute(
                "SELECT COUNT(*) FROM reference_memories"
            ).fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        return {
            "schemaVersion": SCHEMA_VERSION,
            "sqliteUserVersion": version,
            "path": str(self.path),
            "counts": counts,
            "contextBudgetCharacters": MAX_CONTEXT_CHARACTERS,
        }

    def run_retention(self, *, now: datetime | None = None) -> dict[str, int]:
        timestamp = (now or datetime.now(UTC)).isoformat()
        deleted_cutoff = ((now or datetime.now(UTC)) - timedelta(days=DELETED_RETENTION_DAYS)).isoformat()
        removed = {
            "compactedConversationTurns": 0,
            "expiredEvents": 0,
            "deletedRecords": 0,
            "overflowEvents": 0,
        }
        with self._lock, self._connect() as connection:
            compact_before = (
                (now or datetime.now(UTC))
                - timedelta(days=CONVERSATION_COMPACTION_DAYS)
            ).isoformat()
            removed["compactedConversationTurns"] = self._compact_conversations(
                connection,
                compact_before,
            )
            expired = connection.execute(
                "SELECT id FROM events WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (timestamp,),
            ).fetchall()
            removed["expiredEvents"] = len(expired)
            for row in expired:
                connection.execute("DELETE FROM context_search WHERE memory_id = ?", (row["id"],))
            connection.execute(
                "DELETE FROM events WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (timestamp,),
            )
            for table in ("entities", "relationships", "events", "preferences"):
                rows = connection.execute(
                    f"SELECT id FROM {table} WHERE deleted_at IS NOT NULL AND deleted_at <= ?"
                    if table != "preferences"
                    else "SELECT 'preference:' || key AS id FROM preferences WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
                    (deleted_cutoff,),
                ).fetchall()
                removed["deletedRecords"] += len(rows)
                for row in rows:
                    connection.execute("DELETE FROM context_search WHERE memory_id = ?", (row["id"],))
                connection.execute(
                    f"DELETE FROM {table} WHERE deleted_at IS NOT NULL AND deleted_at <= ?",
                    (deleted_cutoff,),
                )
            count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            overflow = max(0, count - MAX_STORED_EVENTS)
            if overflow:
                rows = connection.execute(
                    """
                    SELECT id FROM events
                    ORDER BY importance ASC, occurred_at ASC LIMIT ?
                    """,
                    (overflow,),
                ).fetchall()
                for row in rows:
                    connection.execute("DELETE FROM context_search WHERE memory_id = ?", (row["id"],))
                connection.executemany(
                    "DELETE FROM events WHERE id = ?",
                    ((row["id"],) for row in rows),
                )
                removed["overflowEvents"] = overflow
        self._writes_since_retention = 0
        return removed

    def _compact_conversations(
        self,
        connection: sqlite3.Connection,
        compact_before: str,
    ) -> int:
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE event_type = 'conversation.turn'
              AND deleted_at IS NULL
              AND occurred_at < ?
            ORDER BY occurred_at
            """,
            (compact_before,),
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            day = str(row["occurred_at"])[:10]
            key = str(row["session_id"] or f"day:{day}")
            groups.setdefault(key, []).append(row)
        removed = 0
        for key, group in groups.items():
            if len(group) < 4:
                continue
            user_texts = []
            for row in group:
                data = _decode_json(row["data_json"], {})
                if data.get("role") == "user" and data.get("text"):
                    user_texts.append(str(data["text"])[:240])
            if not user_texts:
                continue
            summary = "Conversation topics: " + "; ".join(user_texts[:8])
            summary = summary[:2_000]
            fingerprint = _fingerprint("conversation.summary", key, group[0]["occurred_at"][:10])
            if not connection.execute(
                "SELECT 1 FROM events WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone():
                event_id = f"event:{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO events(
                        id, event_type, summary, occurred_at, source_id,
                        confidence, importance, expires_at, session_id, turn_id,
                        data_json, fingerprint, created_at, deleted_at
                    ) VALUES (?, 'conversation.summary', ?, ?, 'agent', 1.0,
                              0.65, NULL, ?, NULL, ?, ?, ?, NULL)
                    """,
                    (
                        event_id,
                        summary,
                        group[-1]["occurred_at"],
                        group[0]["session_id"],
                        _json({"turnCount": len(group), "compacted": True}),
                        fingerprint,
                        _now(),
                    ),
                )
                self._index_memory(connection, event_id, "event", summary)
            ids = [row["id"] for row in group]
            connection.executemany(
                "DELETE FROM context_search WHERE memory_id = ?",
                ((event_id,) for event_id in ids),
            )
            connection.executemany(
                "DELETE FROM events WHERE id = ?",
                ((event_id,) for event_id in ids),
            )
            removed += len(ids)
        return removed

    def _after_write(self) -> None:
        self._writes_since_retention += 1
        if self._writes_since_retention >= 100:
            self.run_retention()

    def _record_correction(
        self,
        operation: str,
        target_id: str | None,
        before: Any,
        after: Any,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO corrections(
                    id, operation, target_id, before_json, after_json,
                    source_id, created_at
                ) VALUES (?, ?, ?, ?, ?, 'user', ?)
                """,
                (
                    f"correction:{uuid.uuid4().hex}",
                    operation,
                    target_id,
                    _json(before) if before is not None else None,
                    _json(after) if after is not None else None,
                    _now(),
                ),
            )

    def _record_mentions_from_text(
        self,
        text: str,
        *,
        session_id: str | None,
        turn_id: str | None,
        role: str,
        mentioned_at: str,
    ) -> None:
        normalized = _canonical(text)
        if not normalized:
            return
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, kind, name FROM entities
                WHERE deleted_at IS NULL AND length(name) >= 2
                ORDER BY length(name) DESC LIMIT 500
                """
            ).fetchall()
            for row in rows:
                name = _canonical(row["name"])
                if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", normalized):
                    continue
                connection.execute(
                    """
                    INSERT INTO mentions(
                        id, entity_id, surface, role, session_id, turn_id,
                        mentioned_at, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"mention:{uuid.uuid4().hex}",
                        row["id"],
                        row["name"],
                        role,
                        session_id,
                        turn_id,
                        mentioned_at,
                        row["kind"],
                    ),
                )

    def _recent_mention(
        self,
        kind: str,
        *,
        session_id: str | None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT e.id, e.name, m.session_id, m.mentioned_at
                FROM mentions m JOIN entities e ON e.id = m.entity_id
                WHERE m.kind = ? AND e.deleted_at IS NULL
                ORDER BY CASE WHEN m.session_id = ? THEN 0 ELSE 1 END,
                         m.mentioned_at DESC
                LIMIT 1
                """,
                (kind, session_id),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "mentionedAt": row["mentioned_at"],
            "sessionMatch": bool(session_id and row["session_id"] == session_id),
        }

    def _bound_context_result(
        self,
        result: dict[str, Any],
        *,
        max_characters: int,
    ) -> dict[str, Any]:
        limit = max(1_500, min(max_characters, 20_000))
        result["budget"] = {"maximumCharacters": limit}
        removal_order = ("timeline", "retrievedMemories", "preferences", "resolutions")
        while len(_json(result)) > limit:
            changed = False
            for key in removal_order:
                values = result.get(key)
                if isinstance(values, list) and values:
                    values.pop()
                    changed = True
                    if len(_json(result)) <= limit:
                        break
            if not changed:
                working = result.get("workingContext")
                if isinstance(working, dict) and working:
                    working.pop(next(reversed(working)))
                    changed = True
            if not changed:
                break
        result["budget"]["actualCharacters"] = len(_json(result))
        result["budget"]["truncated"] = result["budget"]["actualCharacters"] >= limit
        return result

    @staticmethod
    def _working_summary(snapshot: dict[str, Any]) -> str:
        app = snapshot.get("currentApplication")
        app_name = app.get("name") if isinstance(app, dict) else app
        document = _path_from_document(snapshot.get("currentDocument"))
        window = snapshot.get("currentWindow")
        parts = []
        if app_name:
            parts.append(f"Using {app_name}")
        if document:
            parts.append(f"with {document.name} open")
        elif window:
            parts.append(f"in {str(window)[:200]}")
        return " ".join(parts) or "Mac working context changed"

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
        memory_id: str | None = None,
    ) -> None:
        key = (phrase.casefold(), target.casefold())
        if key in seen or len(resolutions) >= MAX_RESOLUTIONS:
            return
        seen.add(key)
        value = {
            "phrase": phrase,
            "target": target,
            "kind": kind,
            "source": source,
            "confidence": confidence,
        }
        if memory_id:
            value["memoryId"] = memory_id
        resolutions.append(value)

    @staticmethod
    def _phrase_is_resolved(
        resolutions: list[dict[str, Any]],
        phrase: str,
    ) -> bool:
        return any(
            str(item.get("phrase") or "").casefold() == phrase.casefold()
            for item in resolutions
        )

    @staticmethod
    def _index_memory(
        connection: sqlite3.Connection,
        memory_id: str,
        kind: str,
        content: str,
    ) -> None:
        connection.execute("DELETE FROM context_search WHERE memory_id = ?", (memory_id,))
        connection.execute(
            "INSERT INTO context_search(memory_id, memory_kind, content) VALUES (?, ?, ?)",
            (memory_id, kind, content[:20_000]),
        )

    def _reindex_entity(self, connection: sqlite3.Connection, entity_id: str) -> None:
        row = connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if not row:
            return
        aliases = connection.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?",
            (entity_id,),
        ).fetchall()
        self._index_memory(
            connection,
            entity_id,
            "entity",
            " ".join(
                [row["name"], row["kind"], row["attributes_json"]]
                + [alias["alias"] for alias in aliases]
            ),
        )

    def _memory_by_id(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> dict[str, Any] | None:
        if memory_id.startswith("entity:"):
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
            if row:
                return {**self._row_to_entity(row), "type": "entity"}
        if memory_id.startswith("relationship:"):
            row = connection.execute(
                """
                SELECT r.*, s.name AS subject_name, o.name AS object_name
                FROM relationships r
                JOIN entities s ON s.id = r.subject_id
                LEFT JOIN entities o ON o.id = r.object_id
                WHERE r.id = ? AND r.deleted_at IS NULL
                """,
                (memory_id,),
            ).fetchone()
            if row:
                return {**self._row_to_relationship(row), "type": "relationship"}
        if memory_id.startswith("event:"):
            row = connection.execute(
                "SELECT * FROM events WHERE id = ? AND deleted_at IS NULL",
                (memory_id,),
            ).fetchone()
            if row:
                return {**self._row_to_event(row), "type": "event"}
        if memory_id.startswith("preference:"):
            row = connection.execute(
                "SELECT * FROM preferences WHERE key = ? AND deleted_at IS NULL",
                (memory_id.removeprefix("preference:"),),
            ).fetchone()
            if row:
                return {**self._row_to_preference(row), "type": "preference"}
        return None

    @staticmethod
    def _entity_by_id(
        connection: sqlite3.Connection,
        entity_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        return ContextStore._row_to_entity(row) if row else None

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "attributes": _decode_json(row["attributes_json"], {}),
            "source": row["source_id"],
            "confidence": row["confidence"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _row_to_relationship(row: sqlite3.Row) -> dict[str, Any]:
        value = {
            "id": row["id"],
            "subjectId": row["subject_id"],
            "predicate": row["predicate"],
            "objectId": row["object_id"],
            "objectValue": _decode_json(row["object_value_json"], None),
            "source": row["source_id"],
            "confidence": row["confidence"],
            "observedAt": row["observed_at"],
            "validFrom": row["valid_from"],
            "validUntil": row["valid_until"],
            "metadata": _decode_json(row["metadata_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if "subject_name" in row:
            value["subject"] = row["subject_name"]
            value["object"] = row["object_name"] or value["objectValue"]
        return value

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": row["event_type"],
            "summary": row["summary"],
            "occurredAt": row["occurred_at"],
            "source": row["source_id"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "expiresAt": row["expires_at"],
            "sessionId": row["session_id"],
            "turnId": row["turn_id"],
            "data": _decode_json(row["data_json"], {}),
            "createdAt": row["created_at"],
        }

    @staticmethod
    def _row_to_preference(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": f"preference:{row['key']}",
            "key": row["key"],
            "value": _decode_json(row["value_json"], None),
            "source": row["source_id"],
            "confidence": row["confidence"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _row_to_reference(row: sqlite3.Row) -> dict[str, Any]:
        metadata = _decode_json(row["metadata_json"], {})
        return {
            "id": f"reference:{row['alias'].casefold()}",
            "alias": row["alias"],
            "target": row["target"],
            "kind": row["kind"],
            "metadata": metadata,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "useCount": row["use_count"],
        }


store = ContextStore()
