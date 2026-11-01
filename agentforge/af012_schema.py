"""AF-012 PostgreSQL model definitions; migrations control schema creation."""
from uuid import uuid4

from sqlalchemy import (CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON,
                        String, Text, UniqueConstraint, Uuid, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_message_at = mapped_column(DateTime(timezone=True))
    archived_at = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("status IN ('active','archived')", name="conversation_status"),)


class RepositoryMetadata(Base):
    """A local registry entry; the SQLite code index remains the source of file data."""
    __tablename__ = "repository_metadata"
    repository_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    canonical_path: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    turn_id = mapped_column(Uuid, nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    request_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code = mapped_column(String(128))
    model_name = mapped_column(String(512))
    token_count = mapped_column(Integer)
    message_metadata = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint("conversation_id", "sequence_number"),
                      UniqueConstraint("conversation_id", "turn_id", "role"),
                      CheckConstraint("sequence_number > 0", name="message_sequence_positive"),
                      CheckConstraint("role IN ('user','assistant','system')", name="message_role"),
                      CheckConstraint("request_status IN ('pending','completed','failed')", name="message_status"))


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    covered_through: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_message_id = mapped_column(Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    fact_ledger = mapped_column(JSON, nullable=True)
    model_name = mapped_column(String(512))
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint("conversation_id", "covered_through"),
                      CheckConstraint("covered_through > 0", name="summary_sequence_positive"))


class ConversationSelection(Base):
    __tablename__ = "conversation_selections"
    repository_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    conversation_id = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)


class Memory(Base):
    __tablename__ = "memories"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = mapped_column(DateTime(timezone=True))
    revoked_at = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("approval_status IN ('pending','approved','rejected','revoked','expired')", name="memory_status"),
                      CheckConstraint("version > 0", name="memory_version_positive"))


class MemorySource(Base):
    __tablename__ = "memory_sources"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    memory_id = mapped_column(Uuid, ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False, index=True)
    message_id = mapped_column(Uuid, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    source_sequence = mapped_column(Integer, nullable=False)
    source_role = mapped_column(String(16), nullable=False)
    source_hash = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("memory_id", "source_sequence"),
                      CheckConstraint("source_sequence > 0", name="memory_source_sequence_positive"),
                      CheckConstraint("source_role IN ('user','assistant')", name="memory_source_role"))


class MemoryApproval(Base):
    __tablename__ = "memory_approvals"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    memory_id = mapped_column(Uuid, ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (CheckConstraint("action IN ('approved','rejected','revoked')", name="approval_action"),)


class MemoryEvent(Base):
    __tablename__ = "memory_events"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    memory_id = mapped_column(Uuid, ForeignKey("memories.id", ondelete="RESTRICT"), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details = mapped_column(JSON, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint("memory_id", "sequence_number"),
                      CheckConstraint("sequence_number > 0", name="memory_event_sequence_positive"))


class VectorSyncOutbox(Base):
    __tablename__ = "vector_sync_outbox"
    id = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error = mapped_column(String(128))
    created_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint("repository_id", "source_type", "source_id", "source_version", "action"),
                      CheckConstraint("source_version > 0 AND attempts >= 0", name="outbox_bounds"),
                      CheckConstraint("action IN ('upsert','delete')", name="outbox_action"),
                      CheckConstraint("status IN ('pending','processing','completed','dead')", name="outbox_status"),
                      Index("ix_vector_outbox_status_created", "status", "created_at"))
