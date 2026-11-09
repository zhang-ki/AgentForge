"""意图路由：把用户请求映射为对话能力组合，不执行检索或模型调用。"""

from dataclasses import dataclass
from enum import Enum

from .planner import ConversationPlan, ConversationTask


class DialogueMode(str, Enum):
    GENERAL = "general"
    REPOSITORY = "repository"
    HYBRID = "hybrid"
    AUTO = "auto"


@dataclass(frozen=True)
class DialoguePlan:
    mode: DialogueMode
    use_repository: bool
    use_general_knowledge: bool
    reason: str
    tasks: tuple[ConversationTask, ...] = ()
    requires_external_data: bool = False
    requires_agent_execution: bool = False
    answer_strategy: str = "direct"
    clarification_required: bool = False
    confidence: float | None = None
    planner_source: str = "explicit"


def plan_dialogue(requested: str | DialogueMode, question: str, *,
                  planner_result: ConversationPlan | None = None) -> DialoguePlan:
    """应用显式用户模式，或把经过 Schema 校验的 LLM 计划转成执行计划。"""
    try:
        mode = requested if isinstance(requested, DialogueMode) else DialogueMode(requested)
    except (ValueError, TypeError):
        raise ValueError("Dialogue mode must be general, repository, hybrid or auto.") from None
    if not isinstance(question, str) or not question.strip() or len(question) > 32_000:
        raise ValueError("A non-empty bounded dialogue question is required.")
    if mode in {DialogueMode.AUTO, DialogueMode.HYBRID}:
        if not isinstance(planner_result, ConversationPlan):
            raise ValueError("Auto and Hybrid modes require a validated LLM Planner result.")
        if mode is DialogueMode.HYBRID and planner_result.intent != DialogueMode.HYBRID.value:
            raise ValueError("The LLM Planner result conflicts with explicit Hybrid mode.")
        mode = DialogueMode(planner_result.intent)
        tasks = planner_result.tasks
        reason = planner_result.reason
        external = planner_result.requires_external_data
        requires_agent_execution = planner_result.requires_agent_execution
        answer_strategy = planner_result.answer_strategy
        clarification = planner_result.clarification_required
        confidence = planner_result.confidence
        source = "llm"
    else:
        reason = f"The user explicitly selected {mode.value} mode."
        external, requires_agent_execution, answer_strategy, clarification, confidence, source = (
            False, False, "direct", False, None, "explicit",
        )
        if planner_result is not None:
            raise ValueError("An LLM Planner result is not accepted for a fixed explicit mode.")
        task_type = "general" if mode is DialogueMode.GENERAL else "repository"
        tasks = (ConversationTask(
            "task_1", task_type, question.strip(), task_type == "repository",
            () if task_type == "general" else (question.strip(),), (),
        ),)
    return DialoguePlan(
        mode=mode,
        use_repository=mode in {DialogueMode.REPOSITORY, DialogueMode.HYBRID},
        use_general_knowledge=any(task.task_type == "general" for task in tasks),
        reason=reason,
        tasks=tasks,
        requires_external_data=external,
        requires_agent_execution=requires_agent_execution,
        answer_strategy=answer_strategy,
        clarification_required=clarification,
        confidence=confidence,
        planner_source=source,
    )


__all__ = ["DialogueMode", "DialoguePlan", "plan_dialogue"]
