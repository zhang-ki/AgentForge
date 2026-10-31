"""PostgreSQL-backed, explicitly approved long-term memory and vector outbox."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from .af012_schema import (
    Conversation, Memory, MemoryApproval, MemoryEvent, MemorySource, Message,
    RepositoryMetadata, VectorSyncOutbox,
)


_MEMORY_TYPES = {"preference", "project_fact", "workflow", "other"}
_SECRET_LIKE = re.compile(
    r"(?i)(?:api[_-]?key|password|passwd|secret|access[_-]?token|private[_-]?key)\s*[:=]"
    r"\s*\S+"
)
_MAX_OUTBOX_ATTEMPTS = 5


def _valid_text(value, name, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or "\x00" in value):
        raise ValueError(f"Invalid {name}.")
    value = value.strip()
    if _SECRET_LIKE.search(value):
        raise ValueError("Memory text appears to contain a credential; it was not stored.")
    return value


def _uuid(value, name):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"Invalid {name}.") from None


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _memory(row):
    return {"id": str(row.id), "repository_id": row.repository_id,
            "memory_type": row.memory_type, "content": row.content,
            "approval_status": row.approval_status, "version": row.version,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "expires_at": row.expires_at, "revoked_at": row.revoked_at}


class MemoryStore:
    """All durable memory changes are scoped and audited in PostgreSQL."""

    def __init__(self, engine):
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)

    @staticmethod
    def register_repository_id(repository_id, canonical_path):
        if not isinstance(repository_id, str) or not repository_id.strip() or len(repository_id) > 256:
            raise ValueError("Invalid repository ID.")
        if not isinstance(canonical_path, str) or not canonical_path.strip() or len(canonical_path) > 2048:
            raise ValueError("Invalid repository path.")
        return repository_id.strip(), canonical_path

    def register_repository(self, repository_id, canonical_path, display_name=None):
        repository_id, canonical_path = self.register_repository_id(repository_id, canonical_path)
        name = _valid_text(display_name or canonical_path.rstrip("\\/").split("\\")[-1],
                           "repository name", 512)
        with self.sessions.begin() as session:
            row = session.get(RepositoryMetadata, repository_id)
            if row is None:
                row = RepositoryMetadata(repository_id=repository_id,
                                         canonical_path=canonical_path, display_name=name)
                session.add(row)
            else:
                if row.canonical_path.casefold() != canonical_path.casefold():
                    raise ValueError("Repository ID is already bound to a different path.")
                row.display_name = name
                row.updated_at = _now()
            session.flush()
            return {"repository_id": row.repository_id, "canonical_path": row.canonical_path,
                    "display_name": row.display_name}

    def propose(self, repository_id, conversation_id, content, source_message_ids, *,
                memory_type="other", expires_at=None):
        """Store a pending suggestion; it is never made active without approval."""
        if memory_type not in _MEMORY_TYPES:
            raise ValueError("Unsupported memory type.")
        content = _valid_text(content, "memory content", 4000)
        if not isinstance(source_message_ids, (list, tuple)) or not 1 <= len(source_message_ids) <= 20:
            raise ValueError("Memory requires 1 to 20 source messages.")
        source_ids = list(dict.fromkeys(_uuid(value, "source message ID") for value in source_message_ids))
        if len(source_ids) != len(source_message_ids):
            raise ValueError("Duplicate memory source messages are not allowed.")
        if expires_at is not None and (not isinstance(expires_at, datetime) or expires_at.tzinfo is None):
            raise ValueError("Memory expiration must be a timezone-aware datetime.")
        now = _now()
        if expires_at is not None and expires_at <= now:
            raise ValueError("Memory expiration must be in the future.")
        memory_id = uuid4()
        repo_id = repository_id.strip() if isinstance(repository_id, str) else ""
        if not repo_id or len(repo_id) > 256:
            raise ValueError("Invalid repository ID.")
        conversation_uuid = _uuid(conversation_id, "conversation ID")
        with self.sessions.begin() as session:
            conversation = session.scalar(select(Conversation).where(
                Conversation.id == conversation_uuid, Conversation.repository_id == repo_id))
            if conversation is None:
                raise ValueError("Conversation not found in the requested repository.")
            source_rows = list(session.scalars(select(Message).where(
                Message.conversation_id == conversation_uuid, Message.id.in_(source_ids))))
            by_id = {row.id: row for row in source_rows}
            if len(by_id) != len(source_ids) or any(
                    by_id[identity].request_status != "completed"
                    or by_id[identity].role not in ("user", "assistant") for identity in source_ids):
                raise ValueError("Every source message must be completed and belong to this conversation.")
            memory = Memory(id=memory_id, repository_id=repo_id, memory_type=memory_type,
                            content=content, approval_status="pending", version=1,
                            expires_at=expires_at)
            session.add(memory)
            for identity in source_ids:
                row = by_id[identity]
                source_hash = hashlib.sha256((row.role + "\0" + row.content).encode("utf-8")).hexdigest()
                session.add(MemorySource(id=uuid4(), memory_id=memory_id, message_id=row.id,
                    source_sequence=row.sequence_number, source_role=row.role, source_hash=source_hash))
            self._event(session, memory, "proposed", {"source_count": len(source_ids)})
            session.flush()
            return _memory(memory)

    def _scoped_memory(self, session, memory_id, repository_id, *, lock=False):
        query = select(Memory).where(Memory.id == _uuid(memory_id, "memory ID"),
                                     Memory.repository_id == repository_id)
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise ValueError("Memory not found in the current repository.")
        return row

    @staticmethod
    def _event(session, memory, event_type, details):
        current = session.scalar(select(func.max(MemoryEvent.sequence_number)).where(
            MemoryEvent.memory_id == memory.id)) or 0
        session.add(MemoryEvent(id=uuid4(), memory_id=memory.id,
            sequence_number=current + 1, event_type=event_type, details=details))

    def get(self, memory_id, repository_id):
        with self.sessions() as session:
            return _memory(self._scoped_memory(session, memory_id, repository_id))

    def list(self, repository_id, *, status=None, limit=100):
        if not isinstance(repository_id, str) or not repository_id.strip():
            raise ValueError("Repository ID is required.")
        if status is not None and status not in {"pending", "approved", "rejected", "revoked", "expired"}:
            raise ValueError("Invalid memory status.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Memory list limit must be between 1 and 500.")
        query = select(Memory).where(Memory.repository_id == repository_id)
        if status:
            query = query.where(Memory.approval_status == status)
        with self.sessions() as session:
            rows = session.scalars(query.order_by(Memory.updated_at.desc(), Memory.id).limit(limit))
            return [_memory(row) for row in rows]

    def _queue(self, session, row, action):
        session.add(VectorSyncOutbox(id=uuid4(), repository_id=row.repository_id,
            source_type="memory", source_id=str(row.id), source_version=row.version,
            action=action, status="pending", attempts=0, last_error=None))

    def approve(self, memory_id, repository_id):
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            if row.approval_status != "pending":
                raise ValueError("Only pending memory suggestions can be approved.")
            now = _now()
            if row.expires_at is not None and _utc(row.expires_at) <= now:
                raise ValueError("Expired memory cannot be approved.")
            row.approval_status, row.updated_at = "approved", now
            session.add(MemoryApproval(id=uuid4(), memory_id=row.id, action="approved"))
            self._event(session, row, "approved", {})
            self._queue(session, row, "upsert")
            session.flush()
            return _memory(row)

    def reject(self, memory_id, repository_id):
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            if row.approval_status != "pending":
                raise ValueError("Only pending memory suggestions can be rejected.")
            row.approval_status, row.updated_at = "rejected", _now()
            session.add(MemoryApproval(id=uuid4(), memory_id=row.id, action="rejected"))
            self._event(session, row, "rejected", {})
            session.flush()
            return _memory(row)

    def update(self, memory_id, repository_id, content, *, memory_type=None, expires_at=None):
        content = _valid_text(content, "memory content", 4000)
        if memory_type is not None and memory_type not in _MEMORY_TYPES:
            raise ValueError("Unsupported memory type.")
        if expires_at is not None and (not isinstance(expires_at, datetime) or expires_at.tzinfo is None
                                       or expires_at <= _now()):
            raise ValueError("Memory expiration must be a future timezone-aware datetime.")
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            if row.approval_status in {"revoked", "expired"}:
                raise ValueError("Revoked or expired memory cannot be updated.")
            row.version += 1
            row.content, row.updated_at, row.expires_at = content, _now(), expires_at
            if memory_type is not None:
                row.memory_type = memory_type
            row.approval_status, row.revoked_at = "pending", None
            self._event(session, row, "updated_pending_reapproval", {"version": row.version})
            self._queue(session, row, "delete")
            session.flush()
            return _memory(row)

    def revoke(self, memory_id, repository_id):
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            if row.approval_status not in {"approved", "pending"}:
                raise ValueError("Only active or pending memory can be revoked.")
            row.version += 1
            row.approval_status, row.revoked_at, row.updated_at = "revoked", _now(), _now()
            session.add(MemoryApproval(id=uuid4(), memory_id=row.id, action="revoked"))
            self._event(session, row, "revoked", {"version": row.version})
            self._queue(session, row, "delete")
            session.flush()
            return _memory(row)

    def expire_due(self, *, limit=500):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("Expiration batch limit is invalid.")
        now = _now()
        with self.sessions.begin() as session:
            rows = list(session.scalars(select(Memory).where(
                Memory.approval_status == "approved", Memory.expires_at.is_not(None),
                Memory.expires_at <= now).order_by(Memory.expires_at, Memory.id)
                .limit(limit).with_for_update(skip_locked=True)))
            for row in rows:
                row.version += 1
                row.approval_status, row.revoked_at, row.updated_at = "expired", now, now
                self._event(session, row, "expired", {"version": row.version})
                self._queue(session, row, "delete")
            return len(rows)

    def approved(self, repository_id, *, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Approved memory limit must be between 1 and 100.")
        now = _now()
        with self.sessions() as session:
            rows = session.scalars(select(Memory).where(
                Memory.repository_id == repository_id,
                Memory.approval_status == "approved",
                (Memory.expires_at.is_(None) | (Memory.expires_at > now)))
                .order_by(Memory.updated_at.desc(), Memory.id).limit(limit))
            return [_memory(row) for row in rows]

    def approved_by_id(self, memory_id, repository_id):
        """Resolve an authoritative active memory for Chroma candidate validation."""
        with self.sessions() as session:
            row = self._scoped_memory(session, memory_id, repository_id)
            if (row.approval_status != "approved"
                    or row.expires_at is not None and _utc(row.expires_at) <= _now()):
                return None
            return _memory(row)

    def source_records(self, memory_id, repository_id):
        with self.sessions() as session:
            row = self._scoped_memory(session, memory_id, repository_id)
            sources = session.scalars(select(MemorySource).where(
                MemorySource.memory_id == row.id).order_by(MemorySource.source_sequence))
            return [{"message_id": str(item.message_id) if item.message_id else None,
                     "source_sequence": item.source_sequence, "source_role": item.source_role,
                     "source_hash": item.source_hash} for item in sources]

    def events(self, memory_id, repository_id):
        with self.sessions() as session:
            row = self._scoped_memory(session, memory_id, repository_id)
            events = session.scalars(select(MemoryEvent).where(
                MemoryEvent.memory_id == row.id).order_by(MemoryEvent.sequence_number))
            return [{"event_type": item.event_type, "details": dict(item.details or {}),
                     "created_at": item.created_at} for item in events]

    def sync_outbox(self, vector_store, embedding_provider, *, limit=50):
        """Attempt each claimed item once. Retry is explicit on a later invocation."""
        from .embeddings.base import embed_text
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Outbox batch limit must be between 1 and 500.")
        now = _now()
        with self.sessions.begin() as session:
            stale_before = now - timedelta(minutes=5)
            session.execute(update(VectorSyncOutbox).where(
                VectorSyncOutbox.status == "processing",
                VectorSyncOutbox.updated_at < stale_before).values(status="pending", updated_at=now))
            jobs = list(session.scalars(select(VectorSyncOutbox).where(
                VectorSyncOutbox.status == "pending").order_by(
                    VectorSyncOutbox.created_at, VectorSyncOutbox.id).limit(limit)
                .with_for_update(skip_locked=True)))
            claimed = []
            for job in jobs:
                job.status = "processing"
                job.attempts += 1
                job.updated_at = now
                claimed.append({"id": job.id, "repository_id": job.repository_id,
                    "source_id": job.source_id, "source_version": job.source_version,
                    "action": job.action, "attempts": job.attempts})
        completed = failed = dead = skipped_stale = 0
        for job in claimed:
            try:
                with self.sessions() as session:
                    row = session.scalar(select(Memory).where(
                        Memory.id == UUID(job["source_id"]),
                        Memory.repository_id == job["repository_id"]))
                    if row is None:
                        raise ValueError("missing_source")
                    current_version = row.version
                    active = row.approval_status == "approved" and (
                        row.expires_at is None or _utc(row.expires_at) > _now())
                    content = row.content
                    memory_type = row.memory_type
                if job["source_version"] != current_version:
                    skipped_stale += 1
                    self._finish_outbox(job["id"], "completed", None)
                    continue
                from .embeddings.chroma_store import ChromaSource
                source = ChromaSource(job["repository_id"], "memory", job["source_id"], "",
                    hashlib.sha256(content.encode("utf-8")).hexdigest(), embedding_provider.model_id)
                if job["action"] == "delete" or not active:
                    vector_store.delete(source)
                else:
                    tagged = f"[{memory_type}] {content}"
                    vector_store.upsert(source, embed_text(embedding_provider, tagged, role="document"),
                                        document=tagged)
                self._finish_outbox(job["id"], "completed", None)
                completed += 1
            except Exception as exc:
                status = "dead" if job["attempts"] >= _MAX_OUTBOX_ATTEMPTS else "pending"
                self._finish_outbox(job["id"], status, type(exc).__name__[:128])
                failed += 1
                dead += int(status == "dead")
        return {"claimed": len(claimed), "completed": completed,
                "failed": failed, "dead": dead, "stale_skipped": skipped_stale}

    def _finish_outbox(self, job_id, status, error):
        with self.sessions.begin() as session:
            job = session.get(VectorSyncOutbox, job_id)
            if job is not None and job.status == "processing":
                job.status, job.last_error, job.updated_at = status, error, _now()

    def outbox_status(self, repository_id):
        from sqlalchemy import func
        with self.sessions() as session:
            rows = session.execute(select(VectorSyncOutbox.status, func.count()).where(
                VectorSyncOutbox.repository_id == repository_id,
                VectorSyncOutbox.source_type == "memory").group_by(VectorSyncOutbox.status))
            return {status: count for status, count in rows}
