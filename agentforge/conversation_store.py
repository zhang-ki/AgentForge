"""Transactional conversation storage; independent of the read-only RAG index.

The caller supplies a configured SQLAlchemy engine. PostgreSQL is the supported
deployment target. SQLite is useful for isolated tests, not PostgreSQL evidence.
"""
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .af012_schema import Conversation, ConversationSelection, ConversationSummary, Message


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"Invalid {name}.")
    return value.strip()


def _id(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid conversation ID.") from None


def _conversation(row):
    return {"id": str(row.id), "repository_id": row.repository_id, "title": row.title,
            "status": row.status, "created_at": row.created_at, "updated_at": row.updated_at,
            "last_message_at": row.last_message_at}


def _message(row):
    return {"id": str(row.id), "conversation_id": str(row.conversation_id),
            "turn_id": str(row.turn_id), "sequence": row.sequence_number,
            "role": row.role, "content": row.content, "status": row.request_status,
            "model_name": row.model_name,
            "created_at": row.created_at, "token_count": row.token_count,
            "metadata": dict(row.message_metadata or {})}


class ConversationStore:
    """Scope reads and writes to one indexed repository, with atomic turns."""
    def __init__(self, engine):
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def _get(self, session, conversation_id, repository_id, *, lock=False):
        repo = _text(repository_id, "repository ID", 256)
        query = select(Conversation).where(Conversation.id == _id(conversation_id),
                                           Conversation.repository_id == repo)
        if lock:
            query = query.with_for_update()
        row = session.scalar(query)
        if row is None:
            raise ValueError("Conversation not found in the current repository.")
        return row

    def create_conversation(self, repository_id, title):
        with self.sessions.begin() as session:
            row = Conversation(id=uuid4(), repository_id=_text(repository_id, "repository ID", 256),
                               title=_text(title, "title", 512), status="active")
            session.add(row)
            session.flush()
            return _conversation(row)

    def list_conversations(self, repository_id):
        repo = _text(repository_id, "repository ID", 256)
        with self.sessions() as session:
            rows = session.scalars(select(Conversation).where(Conversation.repository_id == repo)
                                   .order_by(Conversation.updated_at.desc(), Conversation.id))
            return [_conversation(row) for row in rows]

    def get_conversation(self, conversation_id, repository_id):
        with self.sessions() as session:
            return _conversation(self._get(session, conversation_id, repository_id))

    def switch_conversation(self, conversation_id, repository_id):
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            selection = session.get(ConversationSelection, row.repository_id)
            if selection is None:
                session.add(ConversationSelection(repository_id=row.repository_id, conversation_id=row.id))
            else:
                selection.conversation_id = row.id
            session.flush()
            return _conversation(row)

    def current_conversation(self, repository_id):
        repo = _text(repository_id, "repository ID", 256)
        with self.sessions() as session:
            selection = session.get(ConversationSelection, repo)
            return None if selection is None else _conversation(self._get(session, selection.conversation_id, repo))

    def rename_conversation(self, conversation_id, repository_id, title):
        title = _text(title, "title", 512)
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            row.title, row.updated_at = title, datetime.now(timezone.utc)
            session.flush()
            return _conversation(row)

    def delete_conversation(self, conversation_id, repository_id):
        """Delete only the explicitly identified conversation and its messages."""
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            session.delete(row)

    def append_turn(self, conversation_id, repository_id, question, answer, *, model_name=None):
        question = _text(question, "question", 100_000)
        answer = _text(answer, "answer", 100_000)
        if model_name is not None:
            model_name = _text(model_name, "model", 512)
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            if row.status != "active":
                raise ValueError("Cannot append to an archived conversation.")
            highest = session.scalar(select(func.max(Message.sequence_number)).where(
                Message.conversation_id == row.id)) or 0
            turn_id = uuid4()
            for offset, role, content in ((1, "user", question), (2, "assistant", answer)):
                session.add(Message(id=uuid4(), conversation_id=row.id, turn_id=turn_id,
                                    sequence_number=highest + offset, role=role,
                                    content=content, request_status="completed",
                                    model_name=model_name if role == "assistant" else None,
                                    message_metadata={}))
            row.last_message_at = row.updated_at = datetime.now(timezone.utc)
            session.flush()
            return str(turn_id)

    def get_messages(self, conversation_id, repository_id):
        with self.sessions() as session:
            row = self._get(session, conversation_id, repository_id)
            return [_message(message) for message in session.scalars(
                select(Message).where(Message.conversation_id == row.id)
                .order_by(Message.sequence_number))]

    def save_summary(self, conversation_id, repository_id, covered_message_id, text):
        text = _text(text, "summary", 100_000)
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            covered = session.scalar(select(Message).where(Message.id == _id(covered_message_id),
                                                           Message.conversation_id == row.id))
            if covered is None or covered.role != "assistant" or covered.request_status != "completed":
                raise ValueError("Summary must end at a completed assistant message in this conversation.")
            summary = session.scalar(select(ConversationSummary).where(
                ConversationSummary.conversation_id == row.id,
                ConversationSummary.covered_through == covered.sequence_number))
            if summary is None:
                summary = ConversationSummary(id=uuid4(), conversation_id=row.id,
                    covered_through=covered.sequence_number, covered_message_id=covered.id, content=text)
                session.add(summary)
            else:
                summary.content, summary.updated_at = text, datetime.now(timezone.utc)
            session.flush()
            return str(summary.id)

    def latest_summary(self, conversation_id, repository_id):
        with self.sessions() as session:
            row = self._get(session, conversation_id, repository_id)
            summary = session.scalar(select(ConversationSummary).where(
                ConversationSummary.conversation_id == row.id)
                .order_by(ConversationSummary.covered_through.desc()).limit(1))
            if summary is None:
                return None
            covered = session.scalar(select(Message).where(
                Message.id == summary.covered_message_id,
                Message.conversation_id == row.id,
                Message.sequence_number == summary.covered_through,
                Message.role == "assistant", Message.request_status == "completed"))
            if covered is None or not isinstance(summary.content, str) or not summary.content.strip():
                raise ValueError("Conversation summary is corrupt or belongs to a different conversation.")
            return {"id": str(summary.id), "covered_message_id": str(summary.covered_message_id),
                    "covered_through": summary.covered_through, "content": summary.content}

    def prepare_history(self, conversation_id, repository_id):
        """Create a clearly labelled bounded extract when old history grows long.

        Original messages stay intact. Extracts are incomplete, untrusted context,
        not model-generated summaries or evidence about the repository.
        """
        rows = self.get_messages(conversation_id, repository_id)
        summary = self.latest_summary(conversation_id, repository_id)
        covered = summary["covered_through"] if summary else 0
        remaining = [row for row in rows if row["sequence"] > covered]
        if len(remaining) > 12 and len(remaining) % 2 == 0:
            old = remaining[:-6]
            if all(row["role"] == ("user" if i % 2 == 0 else "assistant")
                   and row["status"] == "completed" for i, row in enumerate(old)):
                # Rebuild from original messages, not a truncated prior summary.
                # The labels identify speakers; they do not promote claims to facts.
                parts = ["Deterministic, incomplete extract of untrusted prior dialogue; no claims are verified."]
                for row in rows:
                    if row["sequence"] > old[-1]["sequence"]:
                        break
                    parts.append(f"{row['role']}: {row['content']}")
                content = "\n".join(parts)
                if len(content.encode("utf-8")) > 6000:
                    raise ValueError("Conversation summary exceeds its safe limit; start a new session or shorten the context.")
                self.save_summary(conversation_id, repository_id, old[-1]["id"], content)
                summary = self.latest_summary(conversation_id, repository_id)
                covered = summary["covered_through"]
                remaining = [row for row in rows if row["sequence"] > covered]
        return remaining, summary
