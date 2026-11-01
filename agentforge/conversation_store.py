"""Transactional conversation storage; independent of the read-only RAG index.

The caller supplies a configured SQLAlchemy engine. PostgreSQL is the supported
deployment target. SQLite is useful for isolated tests, not PostgreSQL evidence.
"""
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .af012_schema import Conversation, ConversationSelection, ConversationSummary, Message
from .conversation_facts import (
    extract_message_facts, merge_fact_ledgers, missing_fact_ids,
    render_fact_section, validate_fact_ledger,
)

_SUMMARY_PREFIX = "Untrusted model-generated conversation summary; not repository evidence."
_SUMMARY_MAX_BYTES = 1_800


def _merge_semantic_summary(previous, update, *, previous_through, current_from, current_through):
    """Store a bounded replacement summary instead of nesting every prior version."""
    if previous is not None and (not isinstance(previous, str) or not previous.strip()):
        raise ValueError("Existing semantic summary is invalid; previous summary was retained.")
    if not isinstance(update, str) or not update.strip():
        raise ValueError("Semantic summary update is empty; previous summary was retained.")
    if current_from > current_through:
        coverage = (f"Compacted prior checkpoint through message {previous_through}; "
                    "no newer messages were added.")
    else:
        coverage = (f"Replacement through message {current_through}; the summarizer received the prior "
                    f"checkpoint through message {previous_through} and new messages "
                    f"{current_from}-{current_through}.")
    merged = f"{_SUMMARY_PREFIX}\n{coverage}\n{update.strip()}"
    if len(merged.encode("utf-8")) > _SUMMARY_MAX_BYTES:
        raise ValueError("Merged semantic summary exceeds its safe limit; existing summary and messages were retained.")
    return merged


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
        self.last_summary_diagnostics = []

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

    def save_summary(self, conversation_id, repository_id, covered_message_id, text, *,
                     model_name=None, fact_ledger=None):
        text = _text(text, "summary", 100_000)
        if len(text.encode("utf-8")) > _SUMMARY_MAX_BYTES:
            raise ValueError("Summary exceeds the bounded storage budget; existing summary was retained.")
        normalized_ledger = None if fact_ledger is None else validate_fact_ledger(fact_ledger)
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            covered = session.scalar(select(Message).where(Message.id == _id(covered_message_id),
                                                           Message.conversation_id == row.id))
            if covered is None or covered.role != "assistant" or covered.request_status != "completed":
                raise ValueError("Summary must end at a completed assistant message in this conversation.")
            summary = session.scalar(select(ConversationSummary).where(
                ConversationSummary.conversation_id == row.id,
                ConversationSummary.covered_through == covered.sequence_number))
            persisted_ledger = validate_fact_ledger(summary.fact_ledger or []) if summary else []
            source_rows = [_message(message) for message in session.scalars(
                select(Message).where(
                    Message.conversation_id == row.id,
                    Message.sequence_number <= covered.sequence_number,
                ).order_by(Message.sequence_number))]
            normalized_ledger = merge_fact_ledgers(
                persisted_ledger, normalized_ledger or [],
                extract_message_facts(source_rows))
            if missing_fact_ids(text, normalized_ledger):
                raise ValueError("Summary omitted a protected source fact; existing summary and cursor were retained.")
            if summary is None:
                summary = ConversationSummary(id=uuid4(), conversation_id=row.id,
                    covered_through=covered.sequence_number, covered_message_id=covered.id,
                    content=text, fact_ledger=normalized_ledger, model_name=model_name)
                session.add(summary)
            else:
                summary.content, summary.updated_at = text, datetime.now(timezone.utc)
                summary.model_name = model_name
                summary.fact_ledger = normalized_ledger
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
            ledger = validate_fact_ledger(summary.fact_ledger or [])
            if missing_fact_ids(summary.content, ledger):
                raise ValueError("Conversation summary lost a protected source fact.")
            for fact in ledger:
                for source in fact["sources"]:
                    try:
                        source_id = UUID(source["message_id"])
                    except (ValueError, TypeError):
                        raise ValueError("Conversation summary fact source identity is invalid.") from None
                    message = session.scalar(select(Message).where(
                        Message.id == source_id, Message.conversation_id == row.id))
                    if (message is None or message.role != "user"
                            or message.request_status != "completed"
                            or message.sequence_number != source["sequence"]
                            or hashlib.sha256(message.content.encode("utf-8")).hexdigest()
                               != source["source_sha256"]
                            or " ".join(fact["text"].split())
                               not in " ".join(message.content.split())):
                        raise ValueError("Conversation summary fact source no longer matches its message.")
            return {"id": str(summary.id), "covered_message_id": str(summary.covered_message_id),
                    "covered_through": summary.covered_through, "content": summary.content,
                    "fact_ledger": ledger,
                    "model_name": summary.model_name}

    def prepare_history(self, conversation_id, repository_id, *, summarizer=None, summary_model=None):
        """Compress older complete turns while retaining all original messages.

        A supplied summarizer receives only the older complete turns selected for
        compression. Its output remains untrusted conversation context, never code
        evidence. Without one, an explicitly labelled deterministic extract is used.
        """
        if summarizer is not None and not callable(summarizer):
            raise ValueError("History summarizer must be callable.")
        self.last_summary_diagnostics = []
        rows = self.get_messages(conversation_id, repository_id)
        summary = self.latest_summary(conversation_id, repository_id)
        covered = summary["covered_through"] if summary else 0
        remaining = [row for row in rows if row["sequence"] > covered]
        # Backfill source-linked statements for summaries created before the
        # ledger migration. Original messages remain authoritative.
        if summary is not None:
            source_rows = [row for row in rows if row["sequence"] <= covered]
            ledger = merge_fact_ledgers(summary.get("fact_ledger", []),
                                        extract_message_facts(source_rows))
            hydrated = render_fact_section(summary["content"], ledger)
            if missing_fact_ids(hydrated, ledger):
                raise ValueError("Existing summary cannot satisfy its source-fact ledger; cursor was not advanced.")
            if (hydrated != summary["content"] or ledger != summary.get("fact_ledger", [])):
                if len(hydrated.encode("utf-8")) > _SUMMARY_MAX_BYTES:
                    if summarizer is None:
                        raise ValueError("Protected conversation facts exceed the summary byte budget; existing summary was retained.")
                    # Keep the hydrated version in memory for a bounded model
                    # compaction. Do not persist it unless the smaller candidate
                    # passes fact coverage and size checks.
                    summary = {**summary, "content": hydrated, "fact_ledger": ledger}
                else:
                    self.save_summary(conversation_id, repository_id, summary["covered_message_id"],
                                      hydrated, model_name=summary["model_name"],
                                      fact_ledger=ledger)
                    summary = self.latest_summary(conversation_id, repository_id)
        # Summaries written by earlier versions could fit the storage cap but
        # exceed the current prompt budget. Compact those legacy summaries on
        # the next explicitly enabled semantic-summary path before planning a
        # user answer. The old row remains intact if generation or validation
        # fails because save_summary is reached only after a valid replacement.
        if (summary is not None and summarizer is not None
                and len(summary["content"].encode("utf-8")) > _SUMMARY_MAX_BYTES):
            source = [{"id": summary["covered_message_id"],
                       "sequence": summary["covered_through"], "role": "summary",
                       "content": summary["content"]}]
            update = summarizer(source)
            if (not isinstance(update, str) or not update.strip()
                    or len(update.encode("utf-8")) > 6000):
                raise ValueError("Semantic summary response is empty or exceeds its safe limit.")
            compacted = _merge_semantic_summary(
                summary["content"], update, previous_through=covered,
                current_from=covered + 1, current_through=covered,
            )
            ledger = summary.get("fact_ledger", [])
            compacted = render_fact_section(compacted, ledger)
            if missing_fact_ids(compacted, ledger):
                raise ValueError("Compacted summary omitted a protected source fact; existing summary was retained.")
            self.save_summary(conversation_id, repository_id,
                              summary["covered_message_id"], compacted,
                              model_name=summary_model or summary["model_name"],
                              fact_ledger=ledger)
            summary = self.latest_summary(conversation_id, repository_id)
            covered = summary["covered_through"]
            remaining = [row for row in rows if row["sequence"] > covered]
        if len(remaining) > 12 and len(remaining) % 2 == 0:
            old = remaining[:-6]
            if all(row["role"] == ("user" if i % 2 == 0 else "assistant")
                   and row["status"] == "completed" for i, row in enumerate(old)):
                # Rebuild the summary incrementally: include the previous
                # untrusted summary plus only messages not covered by it. This
                # prevents each update from resending the whole transcript.
                if summarizer is not None:
                    target_sequence = old[-1]["sequence"]
                    pending_rows = [row for row in rows
                                    if covered < row["sequence"] <= target_sequence]
                    base_source = ([{"id": summary["covered_message_id"],
                        "sequence": summary["covered_through"], "role": "summary",
                        "content": summary["content"]}] if summary is not None else [])
                    # Detect an individually uncompressible turn before making
                    # any model request or persisting an intermediate summary.
                    for offset in range(0, len(pending_rows), 2):
                        pair = pending_rows[offset:offset + 2]
                        candidate = base_source + [
                            {key: row[key] for key in ("id", "sequence", "role", "content")}
                            for row in pair]
                        if len(json.dumps(candidate, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")) > 3_000:
                            self.last_summary_diagnostics.append({
                                "input_json_bytes": len(json.dumps(candidate, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")),
                                "input_sequence_start": pair[0]["sequence"],
                                "input_sequence_end": pair[-1]["sequence"],
                                "summary_coverage_start": covered + 1,
                                "summary_coverage_end": pair[-1]["sequence"],
                                "request_attempted": False, "output_bytes": None,
                                "fact_check": "not_completed", "committed": False,
                                "cursor_before": covered, "cursor_after": covered,
                                "failure_stage": "input_validation",
                                "failure_code": "semantic_summary_turn_input_too_large",
                            })
                            raise ValueError("A complete turn exceeds the bounded semantic summary input; existing summary and messages were retained.")
                    while covered < target_sequence:
                        source = []
                        if summary is not None:
                            source.append({"id": summary["covered_message_id"],
                                "sequence": summary["covered_through"], "role": "summary",
                                "content": summary["content"]})
                        pending = [row for row in rows
                                   if covered < row["sequence"] <= target_sequence]
                        batch = []
                        position = 0
                        while position < len(pending):
                            pair = pending[position:position + 2]
                            if (len(pair) != 2 or pair[0]["role"] != "user"
                                    or pair[1]["role"] != "assistant"
                                    or any(row["status"] != "completed" for row in pair)):
                                raise ValueError("Semantic summary input must contain complete successful turns.")
                            addition = [{key: row[key] for key in
                                         ("id", "sequence", "role", "content")} for row in pair]
                            candidate = source + batch + addition
                            serialized = json.dumps(candidate, ensure_ascii=False,
                                                     separators=(",", ":"))
                            if len(serialized.encode("utf-8")) > 3_000:
                                if not batch:
                                    self.last_summary_diagnostics.append({
                                        "input_json_bytes": len(serialized.encode("utf-8")),
                                        "input_sequence_start": pair[0]["sequence"],
                                        "input_sequence_end": pair[-1]["sequence"],
                                        "summary_coverage_start": covered + 1,
                                        "summary_coverage_end": pair[-1]["sequence"],
                                        "request_attempted": False, "output_bytes": None,
                                        "fact_check": "not_completed", "committed": False,
                                        "cursor_before": covered, "cursor_after": covered,
                                        "failure_stage": "input_validation",
                                        "failure_code": "semantic_summary_turn_input_too_large",
                                    })
                                    raise ValueError("A complete turn exceeds the bounded semantic summary input; existing summary and messages were retained.")
                                break
                            batch.extend(addition)
                            position += 2
                        if not batch:
                            self.last_summary_diagnostics.append({
                                "input_sequence_start": pending[0]["sequence"] if pending else None,
                                "input_sequence_end": pending[0]["sequence"] if pending else None,
                                "summary_coverage_start": covered + 1,
                                "summary_coverage_end": target_sequence,
                                "request_attempted": False, "output_bytes": None,
                                "fact_check": "not_completed", "committed": False,
                                "cursor_before": covered, "cursor_after": covered,
                                "failure_stage": "input_validation",
                                "failure_code": "semantic_summary_no_complete_turn_fits",
                            })
                            raise ValueError("No complete turn fits the bounded semantic summary input; existing summary and messages were retained.")
                        source.extend(batch)
                        call_diag_start = len(getattr(summarizer, "diagnostics", ()))
                        cursor_before = covered
                        covered_message_id_before = (
                            summary["covered_message_id"] if summary else None)
                        try:
                            update = summarizer(source)
                        except Exception as exc:
                            callback_diags = getattr(summarizer, "diagnostics", ())
                            callback_diag = callback_diags[-1] if len(callback_diags) > call_diag_start else {}
                            self.last_summary_diagnostics.append({
                                **callback_diag,
                                "input_sequence_start": batch[0]["sequence"],
                                "input_sequence_end": batch[-1]["sequence"],
                                "request_attempted": callback_diag.get("request_attempted", True),
                                "summary_coverage_start": cursor_before + 1,
                                "summary_coverage_end": batch[-1]["sequence"],
                                "fact_count": len(merge_fact_ledgers(
                                    summary.get("fact_ledger", []) if summary else [],
                                    extract_message_facts(batch))),
                                "fact_check": "not_completed",
                                "committed": False,
                                "cursor_before": cursor_before,
                                "cursor_after": cursor_before,
                                "covered_message_id_before": covered_message_id_before,
                                "covered_message_id_after": covered_message_id_before,
                                "failure_exception_type": callback_diag.get(
                                    "failure_exception_type", type(exc).__name__),
                                "failure_stage": callback_diag.get("failure_stage", "summarizer"),
                                "failure_code": callback_diag.get("failure_code", type(exc).__name__),
                            })
                            raise
                        if (not isinstance(update, str) or not update.strip()
                                or len(update.encode("utf-8")) > 6000):
                            self.last_summary_diagnostics.append({
                                "input_sequence_start": batch[0]["sequence"],
                                "input_sequence_end": batch[-1]["sequence"],
                                "summary_coverage_start": cursor_before + 1,
                                "summary_coverage_end": batch[-1]["sequence"],
                                "output_bytes": len(update.encode("utf-8")) if isinstance(update, str) else None,
                                "fact_check": "not_completed",
                                "committed": False,
                                "cursor_before": cursor_before,
                                "cursor_after": cursor_before,
                                "covered_message_id_before": covered_message_id_before,
                                "covered_message_id_after": covered_message_id_before,
                                "failure_exception_type": "ValueError",
                                "failure_stage": "response_validation",
                                "failure_code": "semantic_summary_response_invalid",
                            })
                            raise ValueError("Semantic summary response is empty or exceeds its safe limit.")
                        ledger = merge_fact_ledgers(
                            summary.get("fact_ledger", []) if summary else [],
                            extract_message_facts(batch),
                        )
                        try:
                            model_content = _merge_semantic_summary(
                                summary["content"] if summary else None,
                                update,
                                previous_through=covered,
                                current_from=(batch[0]["sequence"] if summary is None else covered + 1),
                                current_through=batch[-1]["sequence"],
                            )
                        except Exception as exc:
                            callback_diags = getattr(summarizer, "diagnostics", ())
                            callback_diag = callback_diags[-1] if len(callback_diags) > call_diag_start else {}
                            self.last_summary_diagnostics.append({
                                **callback_diag,
                                "input_sequence_start": batch[0]["sequence"],
                                "input_sequence_end": batch[-1]["sequence"],
                                "summary_coverage_start": cursor_before + 1,
                                "summary_coverage_end": batch[-1]["sequence"],
                                "output_bytes": len(update.encode("utf-8")),
                                "fact_count": len(ledger),
                                "fact_check": "not_completed",
                                "committed": False,
                                "cursor_before": cursor_before,
                                "cursor_after": cursor_before,
                                "covered_message_id_before": covered_message_id_before,
                                "covered_message_id_after": covered_message_id_before,
                                "failure_exception_type": type(exc).__name__,
                                "failure_stage": "summary_composition",
                                "failure_code": ("semantic_summary_merged_output_too_large"
                                    if "exceeds its safe limit" in str(exc) else type(exc).__name__),
                            })
                            raise
                        # The model's compact prose is fallible. The persisted
                        # summary is composed with exact user-source statements
                        # so omissions are visible and never silently erase facts.
                        model_missing = missing_fact_ids(model_content, ledger)
                        content = render_fact_section(model_content, ledger)
                        missing = missing_fact_ids(content, ledger)
                        output_bytes = len(update.encode("utf-8"))
                        input_sequence_start = batch[0]["sequence"]
                        input_sequence_end = batch[-1]["sequence"]
                        callback_diags = getattr(summarizer, "diagnostics", ())
                        callback_diag = callback_diags[-1] if len(callback_diags) > call_diag_start else {}
                        fact_diag = {
                            **callback_diag,
                            "input_sequence_start": input_sequence_start,
                            "input_sequence_end": input_sequence_end,
                            "summary_coverage_start": cursor_before + 1,
                            "summary_coverage_end": input_sequence_end,
                            "output_bytes": output_bytes,
                            "fact_count": len(ledger),
                            "model_output_missing_fact_count": len(model_missing),
                            "fact_check": "passed" if not missing else "failed",
                            "committed": False,
                            "cursor_before": cursor_before,
                            "cursor_after": cursor_before,
                            "covered_message_id_before": covered_message_id_before,
                            "covered_message_id_after": covered_message_id_before,
                            "failure_stage": None,
                            "failure_code": None,
                        }
                        if missing:
                            fact_diag.update({"failure_stage": "fact_validation",
                                              "failure_exception_type": "ValueError",
                                              "failure_code": "semantic_summary_missing_protected_fact"})
                            self.last_summary_diagnostics.append(fact_diag)
                            raise ValueError("Composed summary omitted a protected source fact; existing summary and cursor were retained.")
                        if len(content.encode("utf-8")) > _SUMMARY_MAX_BYTES:
                            fact_diag.update({"fact_check": "passed", "failure_stage": "byte_budget",
                                              "failure_exception_type": "ValueError",
                                              "failure_code": "semantic_summary_fact_protection_overflow"})
                            self.last_summary_diagnostics.append(fact_diag)
                            raise ValueError("Protected conversation facts exceed the summary byte budget; existing summary and cursor were retained.")
                        try:
                            self.save_summary(conversation_id, repository_id, batch[-1]["id"], content,
                                              model_name=summary_model, fact_ledger=ledger)
                        except Exception as exc:
                            message = str(exc)
                            if "protected source fact" in message:
                                persistence_code = "semantic_summary_missing_protected_fact"
                            elif "budget" in message or "exceeds" in message:
                                persistence_code = "semantic_summary_storage_budget_exceeded"
                            else:
                                persistence_code = "semantic_summary_persistence_failed"
                            fact_diag.update({
                                "failure_stage": "persistence",
                                "failure_code": persistence_code,
                                "failure_exception_type": type(exc).__name__,
                                "cursor_after": cursor_before,
                                "covered_message_id_after": covered_message_id_before,
                            })
                            self.last_summary_diagnostics.append(fact_diag)
                            raise
                        summary = self.latest_summary(conversation_id, repository_id)
                        covered = summary["covered_through"]
                        fact_diag.update({"committed": True, "cursor_after": covered,
                                          "covered_message_id_after": batch[-1]["id"]})
                        self.last_summary_diagnostics.append(fact_diag)
                else:
                    parts = ["Deterministic, incomplete extract of untrusted prior dialogue; no claims are verified."]
                    for row in rows:
                        if row["sequence"] > old[-1]["sequence"]:
                            break
                        parts.append(f"{row['role']}: {row['content']}")
                    content = "\n".join(parts)
                    if len(content.encode("utf-8")) > 6000:
                        raise ValueError("Conversation summary exceeds its safe limit; start a new session or shorten the context.")
                    ledger = merge_fact_ledgers(
                        summary.get("fact_ledger", []) if summary else [],
                        extract_message_facts([row for row in rows if row["sequence"] <= old[-1]["sequence"]]),
                    )
                    content = render_fact_section(content, ledger)
                    if len(content.encode("utf-8")) > _SUMMARY_MAX_BYTES:
                        raise ValueError("Protected conversation facts exceed the summary byte budget; existing summary was retained.")
                    self.save_summary(conversation_id, repository_id, old[-1]["id"], content,
                                      fact_ledger=ledger)
                    summary = self.latest_summary(conversation_id, repository_id)
                    covered = summary["covered_through"]
                remaining = [row for row in rows if row["sequence"] > covered]
        return remaining, summary
