"""Application service that runs repository tools for a planned conversation turn."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_loop import AgentLoop, ReadOnlyToolbox
from .agent_execution import (
    LLMExecutionPlanner, PostgresExecutionStateStore, PostgresToolResultStore,
    ToolAuthorization, ToolDiscovery, toolbox_registry,
)


@dataclass(frozen=True)
class ConversationAgentExecution:
    report: object
    provider: object
    planner: object
    toolbox: ReadOnlyToolbox | None


class ConversationAgentExecutor:
    """Bridge a validated dialogue plan to the existing bounded AgentLoop.

    The service owns construction and persistence dependencies; planning and tool
    scheduling remain in LLMExecutionPlanner and AgentExecutionEngine.
    """

    def __init__(self, *, engine=None, max_steps: int = 8,
                 tool_timeout_seconds: float = 30.0,
                 run_timeout_seconds: float = 120.0,
                 tool_registry_factory=None, planner_factory=None,
                 authorization_factory=None):
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 8:
            raise ValueError("Conversation Agent step limit is outside the allowed range.")
        if (isinstance(tool_timeout_seconds, bool)
                or not isinstance(tool_timeout_seconds, (int, float))
                or not 0 < tool_timeout_seconds <= 120):
            raise ValueError("Conversation Agent tool timeout is outside the allowed range.")
        if (isinstance(run_timeout_seconds, bool)
                or not isinstance(run_timeout_seconds, (int, float))
                or not 0 < run_timeout_seconds <= 600
                or tool_timeout_seconds > run_timeout_seconds):
            raise ValueError("Conversation Agent run timeout is outside the allowed range.")
        self.engine = engine
        self.max_steps = max_steps
        self.tool_timeout_seconds = float(tool_timeout_seconds)
        self.run_timeout_seconds = float(run_timeout_seconds)
        for name, factory in (("tool_registry_factory", tool_registry_factory),
                              ("planner_factory", planner_factory),
                              ("authorization_factory", authorization_factory)):
            if factory is not None and not callable(factory):
                raise ValueError(f"{name} must be callable.")
        self.tool_registry_factory = tool_registry_factory
        self.planner_factory = planner_factory
        self.authorization_factory = authorization_factory

    def execution_available_for(self, request) -> bool:
        """Report whether this request can expose an explicitly authorized tool set."""
        if not callable(getattr(self, "execute", None)):
            return False
        if self.tool_registry_factory is not None:
            # Custom registries are only advertised when their permission policy
            # is injected explicitly; registration alone never grants access.
            return self.authorization_factory is not None
        return (isinstance(request.repository, str)
                and bool(request.repository.strip())
                and request.repository.strip().casefold() != "stateless")

    def planner_tool_catalog_for(self, request, *, query: str, repository_id=None,
                                 index_store=None):
        """Return bounded tool metadata the conversation planner may route to.

        This catalog contains only tools exposed by the same registry factory and
        permission policy used by execution. Schemas remain the execution planner's
        responsibility and are loaded only after a validated Agent plan selects a tool.
        """
        if not self.execution_available_for(request):
            return ()
        if not isinstance(query, str) or not query.strip() or len(query) > 32_000:
            raise ValueError("Tool catalog query must be non-empty and bounded.")
        if self.tool_registry_factory is None:
            registry = toolbox_registry(None, include_index=False)
            authorization = (self.authorization_factory(request, repository_id)
                             if self.authorization_factory is not None else
                             ToolAuthorization(
                                 permission_scopes=frozenset({"repository:read"}),
                             ))
        else:
            try:
                registry = self.tool_registry_factory(None)
            except (AttributeError, TypeError, ValueError):
                if (index_store is None or not isinstance(request.repository, str)
                        or not request.repository.strip()
                        or request.repository.strip().casefold() == "stateless"):
                    raise
                # A registry factory may need the same read-only toolbox used at
                # execution time to register repository tools. This preview is
                # metadata-only; ToolDiscovery never invokes handlers.
                toolbox = ReadOnlyToolbox(request.repository, index_store)
                registry = self.tool_registry_factory(toolbox)
            authorization = self.authorization_factory(request, repository_id)
        if not isinstance(authorization, ToolAuthorization):
            raise ValueError("Tool catalog requires the execution authorization policy.")
        catalog = ToolDiscovery(registry).discover(
            query, permission_scopes=authorization.permission_scopes,
        )
        # Keep the first Planner call small. Full schemas are fetched by the
        # Agent planner after it has selected a tool from these authorized IDs.
        return tuple({
            "tool_id": item.tool_id,
            "description": item.description[:96],
            "side_effect": item.side_effect,
            "requires_approval": item.requires_approval,
        } for item in catalog[:8])

    def execute(self, request, *, session, index_store, dialogue_plan,
                streaming, cancel_event=None) -> ConversationAgentExecution:
        if not getattr(dialogue_plan, "requires_agent_execution", False):
            raise ValueError("Agent execution was requested without a validated tool plan.")
        repository_bound = (
            isinstance(request.repository, str)
            and bool(request.repository.strip())
            and request.repository.strip().casefold() != "stateless"
        )
        # The built-in registry contains only the existing bounded repository
        # read tools. Its availability is tied to an explicitly bound/indexed
        # repository, not to the Planner's answer/retrieval intent flag.
        uses_builtin_repository_tools = self.tool_registry_factory is None
        uses_repository = (repository_bound if uses_builtin_repository_tools else
                           bool(getattr(dialogue_plan, "use_repository", False)))
        if uses_repository and (
                index_store is None or not isinstance(request.repository, str)
                or not request.repository.strip()):
            raise ValueError("Repository tool execution requires the explicitly selected repository index.")
        if uses_builtin_repository_tools and not uses_repository:
            raise ValueError("The built-in tool registry requires an explicitly bound repository.")
        if not uses_repository and self.authorization_factory is None:
            raise ValueError("General tool execution requires an explicitly authorized tool registry.")
        if request.provider_id == "deepseek" and not request.cloud_data_authorized:
            raise ValueError("DeepSeek Agent execution requires explicit cloud-data authorization.")

        provider = session.provider
        if provider is None or not callable(getattr(provider, "plan_completion", None)):
            raise ValueError("The selected conversation model cannot run the structured Agent planner.")
        repository_id = session.repository_id
        if request.conversation_id and (
                not isinstance(repository_id, str) or not repository_id.strip()):
            raise ValueError("Agent execution requires a repository identity.")
        if request.conversation_id and self.engine is None:
            raise ValueError("Persistent Agent execution requires the active PostgreSQL engine.")

        conversation = session.conversation if isinstance(session.conversation, dict) else {}
        history = tuple({"role": item["role"], "content": item["content"],
                         "status": item.get("status", "completed")}
                        for item in session.rows
                        if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
                        and item.get("status", "completed") == "completed"
                        and isinstance(item.get("content"), str))
        memories = tuple(conversation.get("memories", ()))
        memory_query = (lambda _query: memories) if memories and repository_id else None
        planner_budget = LLMExecutionPlanner.output_tokens_for(provider)
        planner_options = {
            "max_output_tokens": planner_budget,
            # Model-planner latency is independent from an individual tool's
            # execution timeout. In particular, short tool deadlines must not
            # fall below the provider's minimum request-timeout validation.
            "timeout_seconds": LLMExecutionPlanner.TIMEOUT_SECONDS,
            "history": history, "summary": conversation.get("summary"),
            "memory_query": memory_query, "repository_id": repository_id,
            "task_context": dialogue_plan.tasks, "tools_only": True,
        }
        planner = (self.planner_factory(provider, **planner_options)
                   if self.planner_factory is not None
                   else LLMExecutionPlanner(provider, **planner_options))
        if not callable(getattr(planner, "plan", None)):
            raise ValueError("The configured Agent planner must expose plan().")
        authorization = (self.authorization_factory(request, repository_id)
                        if self.authorization_factory is not None else
                        ToolAuthorization(permission_scopes=(frozenset({"repository:read"})
                            if uses_repository else frozenset())))
        if not isinstance(authorization, ToolAuthorization):
            raise ValueError("The configured authorization factory must return ToolAuthorization.")
        toolbox = (ReadOnlyToolbox(request.repository, index_store)
                   if uses_repository else None)
        state_store = result_store = None
        if request.conversation_id:
            state_store = PostgresExecutionStateStore(self.engine)
            result_store = PostgresToolResultStore(self.engine)
        loop = AgentLoop(
            toolbox, planner, max_steps=self.max_steps,
            tool_timeout_seconds=self.tool_timeout_seconds,
            run_timeout_seconds=self.run_timeout_seconds,
            state_store=state_store, result_store=result_store,
            authorization=authorization,
            on_event=streaming, cancel_event=cancel_event,
            repository_id=repository_id, conversation_id=request.conversation_id,
            include_index_tool=False,
            tool_registry_factory=self.tool_registry_factory,
        )
        report = loop.run(request.query, execution_id=request.execution_id,
                          user_input=request.agent_user_input)
        return ConversationAgentExecution(report, provider, planner, toolbox)


__all__ = ["ConversationAgentExecution", "ConversationAgentExecutor"]
