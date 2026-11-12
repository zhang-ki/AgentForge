"""显式的多会话交互入口；不管理模型服务进程。"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys

from . import cli, presentation
from .errors import AgentForgeError


_QWEN_ENDPOINT = "http://127.0.0.1:56572/v1/chat/completions"
_DEFAULT_CLOUD_MODEL = "deepseek-flash"


def _registry():
    from .model_registry import ModelRegistry
    return ModelRegistry()


def _deepseek_client():
    from .credential_store import CredentialStore
    from .providers.deepseek import DeepSeekClient
    key, source = CredentialStore().resolve()
    if not key:
        raise ValueError("DeepSeek 密钥未配置；先运行 /model auth set deepseek。")
    client = DeepSeekClient(key)
    client.set_credential_source(source)
    return client, source


def _model_name(provider_id, model_id):
    if provider_id == "llama-server" and model_id in {"qwen-local", "qwen3.5-4b"}:
        return "Qwen Local"
    if provider_id == "offline":
        return "Offline Evidence"
    try:
        return _registry().get(model_id).display_name
    except ValueError:
        return model_id or "未知"


def _show_model_list(current):
    from .credential_store import CredentialStore
    from .model_registry import LOCAL_QWEN, OFFLINE_PROVIDER
    from .providers.local_health import check_qwen_identity
    from .errors import ProviderError

    registry = _registry()
    credentials = CredentialStore().status()
    qwen_status = "NOT_CONFIGURED"
    llama_key = os.environ.get("AGENTFORGE_LLAMA_API_KEY")
    if llama_key:
        try:
            check_qwen_identity(_QWEN_ENDPOINT, llama_key, LOCAL_QWEN.model_id)
            qwen_status = "CONNECTED"
        except (ValueError, OSError, TimeoutError, ProviderError):
            qwen_status = "UNAVAILABLE"
    rows = [LOCAL_QWEN, OFFLINE_PROVIDER, *registry.all()[2:]]
    print("================ Available Models ================")
    for descriptor in rows:
        if descriptor.provider_id == "llama-server":
            status = qwen_status
        elif descriptor.provider_id == "offline":
            status = "CONFIGURED"
        else:
            status = "CONFIGURED" if credentials["configured"] else "NOT_CONFIGURED"
            if registry.cache_status == "stale":
                status += " (stale model list)"
        current_match = bool(current and current.get("provider_id") == descriptor.provider_id
                             and current.get("model_id") == descriptor.model_id)
        default = descriptor.provider_id == "deepseek" and descriptor.model_id == _DEFAULT_CLOUD_MODEL
        model_type = "Cloud" if descriptor.model_type == "cloud" else (
            "Offline" if descriptor.model_type == "offline" else "Local")
        print(f"\n{descriptor.display_name}\n  ID: {descriptor.key}\n  Provider: {descriptor.provider_id}"
              f"\n  Model ID: {descriptor.model_id}\n  Type: {model_type}\n  Status: {status}"
              f"\n  Context: {descriptor.context_window if descriptor.context_window is not None else 'unknown'}"
              f"\n  Max output: {descriptor.max_output_tokens if descriptor.max_output_tokens is not None else 'unknown'}"
              f"\n  Thinking: {descriptor.supports_thinking if descriptor.supports_thinking is not None else 'unknown'}"
              f"\n  Tool calling: {descriptor.supports_tool_calling if descriptor.supports_tool_calling is not None else 'unknown'}"
              f"\n  Current: {'YES' if current_match else 'NO'}\n  Default: {'YES' if default else 'NO'}")
    print(f"\nDeepSeek model cache: {registry.cache_status}; age={registry.cache_age_seconds} seconds"
          "\nUse /model refresh to explicitly contact the DeepSeek model-list endpoint."
          "\n===================================================")
    if current:
        print("Current model: " + _model_name(current.get("provider_id"), current.get("model_id")))


def _save_model_binding(current, descriptor, *, thinking=False, effort=None, cloud_consent=False):
    args = ["session", "set-model", current["id"], descriptor.provider_id,
            descriptor.model_id, "--thinking", "on" if thinking else "off"]
    if effort:
        args.extend(["--effort", effort])
    if cloud_consent:
        args.append("--cloud-consent")
    return _call(*args)["result"]


def _create_default_conversation(title):
    """Create a DeepSeek-default session only after explicit per-session consent."""
    from .credential_store import CredentialStore

    def create_local_session():
        from .model_registry import LOCAL_QWEN
        from .providers.local_health import check_qwen_identity

        api_key = os.environ.get("AGENTFORGE_LLAMA_API_KEY")
        if not api_key:
            raise ValueError("本地 Qwen 认证未配置；没有创建会话。")
        check_qwen_identity(_QWEN_ENDPOINT, api_key, LOCAL_QWEN.model_id)
        made = _call("session", "create", title)["result"]
        current = _call("session", "switch", made["id"])["result"]
        return _save_model_binding(current, LOCAL_QWEN)

    if not CredentialStore().status().get("configured"):
        print("DeepSeek 尚未配置。可先运行 /model auth set deepseek，或明确选择本地 Qwen。")
        if input("输入 LOCAL 使用本地 Qwen；其他输入将取消：").strip() == "LOCAL":
            return create_local_session()
        print("已取消；没有创建会话。")
        return None

    print("新会话默认使用 DeepSeek Flash；问题、相关仓库证据和必要会话上下文会发送到云端。")
    if input("是否允许该会话向 DeepSeek 发送上述内容？输入 YES 确认：").strip() != "YES":
        if input("如需明确改用本地 Qwen 创建会话，输入 LOCAL；其他输入将取消：").strip() != "LOCAL":
            print("已取消；没有创建会话。")
            return None
        return create_local_session()

    registry = _registry()
    try:
        descriptor = registry.get_for_request(_DEFAULT_CLOUD_MODEL)
    except ValueError:
        client, _source = _deepseek_client()
        refreshed = registry.refresh_deepseek(client)
        descriptor = next((item for item in refreshed
                           if item.model_id == _DEFAULT_CLOUD_MODEL), None)
        if descriptor is None:
            raise ValueError("DeepSeek 当前模型列表中没有 deepseek-flash；没有创建会话。") from None
        descriptor = registry.get_for_request(_DEFAULT_CLOUD_MODEL)

    made = _call("session", "create", title)["result"]
    current = _call("session", "switch", made["id"])["result"]
    current = _save_model_binding(current, descriptor, cloud_consent=True)
    print("新会话已绑定 DeepSeek Flash；云端许可已记录在该会话中。")
    return current


def _switch_model(model_key, current):
    if not current:
        raise ValueError("请先选择或新建会话。")
    registry = _registry()
    if model_key == "default":
        model_key = _DEFAULT_CLOUD_MODEL
    if model_key in {"qwen-local", "qwen3.5-4b"}:
        from .model_registry import LOCAL_QWEN
        from .providers.local_health import check_qwen_identity
        api_key = os.environ.get("AGENTFORGE_LLAMA_API_KEY")
        if not api_key:
            raise ValueError("本地 Qwen 认证未配置；当前会话模型保持不变。")
        check_qwen_identity(_QWEN_ENDPOINT, api_key, LOCAL_QWEN.model_id)
        updated = _save_model_binding(current, LOCAL_QWEN)
        print("已切换到 Qwen Local；会话历史、摘要和记忆已保留。")
        return updated
    if model_key == "offline":
        updated = _save_model_binding(current, registry.get("offline"))
        print("已切换到 Offline Evidence；没有模型网络请求。")
        return updated

    # A cloud model switch is an explicit user action and a separate consent.
    print("切换到 DeepSeek 会把当前问题、相关源码证据以及必要的会话历史和获批记忆发送到云端。")
    if input("确认允许本会话将上述内容发送给 DeepSeek？输入 YES 确认：").strip() != "YES":
        print("已取消；原模型绑定保持不变。")
        return current
    client, _source = _deepseek_client()
    refreshed = registry.refresh_deepseek(client)
    descriptor = next((item for item in refreshed if item.model_id == model_key), None)
    if descriptor is None:
        raise ValueError("DeepSeek 当前模型列表中没有该 ID；原会话绑定保持不变。")
    if descriptor.context_window is None or descriptor.max_output_tokens is None:
        raise ValueError("该模型缺少可验证的上下文或输出限制；原会话绑定保持不变。")
    if descriptor.supports_tool_calling is None:
        # Unknown stays unknown; no tool schema is sent for this model.
        pass
    updated = _save_model_binding(current, descriptor, cloud_consent=True)
    print("已切换到 " + descriptor.display_name + "；云端数据许可已记录于当前会话。")
    return updated


def _update_thinking(current, *, thinking=None, effort=None):
    if not current:
        raise ValueError("请先选择或新建会话。")
    registry = _registry()
    descriptor = registry.get_for_request(current.get("model_id", "qwen-local"))
    if descriptor.provider_id != "deepseek":
        raise ValueError("当前模型不支持 Thinking 设置。")
    new_thinking = bool(current.get("thinking_enabled")) if thinking is None else thinking
    new_effort = (None if thinking is False else current.get("reasoning_effort")) if effort is None else effort
    if new_thinking and descriptor.supports_thinking is not True:
        raise ValueError("当前模型没有已确认的 Thinking 能力。")
    if new_effort is not None and new_effort not in descriptor.supported_efforts:
        raise ValueError("该模型不支持此 reasoning effort。")
    if new_effort is not None:
        new_thinking = True
    updated = _save_model_binding(current, descriptor, thinking=new_thinking, effort=new_effort)
    print(f"Thinking: {'on' if new_thinking else 'off'}; effort: {new_effort or 'default'}")
    return updated


def _call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as exc:
            code = exc.code
    if code:
        raise ValueError(err.getvalue().strip() or "会话操作失败。")
    return json.loads(out.getvalue())


def main():
    try:
        config = _call("session", "current")
        current = config["result"]
        suggestions = []
        chroma_enabled = os.environ.get("AGENTFORGE_CHROMA_ENABLED", "1") != "0"
        stream_enabled = False
        debug_enabled = False
        dialogue_mode = "auto"
        output_budget_mode = "standard"
        repository = config.get("repository")
        if repository:
            print("当前仓库：" + repository)
        else:
            print("当前没有活动仓库。可先使用 /repo register 路径；注册时会自动建立 SQLite 与 Chroma 索引。")
        print("普通问题会由 Planner 自动选择通用知识、仓库检索或混合回答。")
        print("本地向量检索默认启用；Embedding 或 Chroma 不可用时会退回 SQLite 检索并提示状态。")
        print("命令：/new 标题、/list、/switch 编号、/rename 标题、/history、/delete、"
              "/repo list|register 路径|switch 编号或ID|index [ID或路径]|rebuild [ID或路径]|remove ID、"
              "/memory list|suggest|save|approve|update|revoke|sync|rebuild、/memory global list|approve ID|revoke ID、"
              "/chroma status|index|rebuild|on|off、/modellist、/model [ID|default|refresh]、"
              "/model auth set deepseek|status|remove deepseek、/thinking on|off、/effort low|high|max、"
              "/output-budget concise|standard|long|max、/cloud-consent status|revoke、"
              "/stream on|off、/debug on|off、/help、/exit")
        while True:
            config = _call("session", "current")
            if config["repository"] != repository:
                repository = config["repository"]
                print("索引仓库已切换：" + repository)
            current = config["result"]
            model_label = (_model_name(current.get("provider_id"), current.get("model_id"))
                           if current else "DeepSeek Flash（新会话默认，需授权）")
            print("\n当前会话：" + (current["title"] if current else "未选择")
                  + " | 模型：" + model_label + " | 模式：" + dialogue_mode)
            line = input("你：").strip()
            if not line:
                continue
            if line in ("/exit", "exit"):
                break
            try:
                if line == "/list":
                    sessions = _call("session", "list")["result"]
                    for i, item in enumerate(sessions, 1):
                        print(f"{i}. {item['title']} ({item['id']})")
                elif line == "/help":
                    print("/modellist：列出本机模型与已缓存的云端模型")
                    print("/model：查看当前模型；/model <ID> 切换；/model default 选择 DeepSeek Flash；/model qwen-local 选择本地 Qwen；/model refresh 刷新 DeepSeek 模型列表")
                    print("/model auth set deepseek、/model auth status、/model auth remove deepseek：安全管理密钥")
                    print("/thinking on|off 与 /effort low|high|max：仅限模型已确认支持时使用")
                    print("/output-budget concise|standard|long|max：设置当前控制台的回答预算；按所选模型能力生效")
                    print("/cloud-consent status|revoke：查看或撤销当前会话的云端数据许可")
                    print("/stream on|off：DeepSeek SSE 草稿流；最终回答仍需通过引用校验")
                    print("/debug on|off：显示或隐藏内部请求阶段详情；诊断 JSONL 始终保留")
                    print("高级调试覆盖：/mode general|repository|hybrid|auto；默认每轮由 Planner 自动路由。")
                elif line == "/mode":
                    print("当前路由：" + ("自动（LLM Planner）" if dialogue_mode == "auto" else dialogue_mode)
                          + "；切换路由不会重置会话、Memory 或执行状态。")
                elif line.startswith("/mode "):
                    selected_mode = line[len("/mode "):].strip().casefold()
                    if selected_mode not in {"general", "repository", "hybrid", "auto"}:
                        raise ValueError("用法：/mode general|repository|hybrid|auto")
                    dialogue_mode = selected_mode
                    print("后续问题的路由覆盖已设为：" + dialogue_mode
                          + "；会话历史、Memory 和执行状态保持不变。")
                elif line == "/modellist":
                    _show_model_list(current)
                elif line == "/output-budget":
                    print("当前回答预算：" + output_budget_mode)
                elif line.startswith("/output-budget "):
                    selected_budget = line[len("/output-budget "):].strip().casefold()
                    if selected_budget not in {"concise", "standard", "long", "max"}:
                        raise ValueError("用法：/output-budget concise|standard|long|max")
                    output_budget_mode = selected_budget
                    print("当前控制台后续回答预算设为：" + output_budget_mode
                          + "；实际 Token 数仍由所选模型的已验证能力和上下文窗口计算。")
                elif line == "/model":
                    if not current:
                        print("请先选择或新建会话。")
                    else:
                        print(f"当前模型：{_model_name(current.get('provider_id'), current.get('model_id'))} "
                              f"({current.get('provider_id')} / {current.get('model_id')})")
                        if current.get("provider_id") == "deepseek":
                            print("Thinking: " + ("on" if current.get("thinking_enabled") else "off")
                                  + "; effort: " + (current.get("reasoning_effort") or "default"))
                elif line.startswith("/model auth "):
                    parts = line.split()
                    valid_status = parts == ["/model", "auth", "status"]
                    valid_target_action = (len(parts) == 4 and parts[3] == "deepseek"
                                           and parts[2] in {"set", "remove"})
                    if not (valid_status or valid_target_action):
                        raise ValueError("用法：/model auth set deepseek、/model auth status、/model auth remove deepseek")
                    from .credential_store import CredentialStore
                    credentials = CredentialStore()
                    if parts[2] == "set":
                        credentials.set_interactive()
                        print("DeepSeek 密钥已通过当前 Windows 用户的 DPAPI 保护保存；不会显示密钥内容。")
                    elif parts[2] == "status":
                        state = credentials.status()
                        print(f"DeepSeek 密钥：{'已配置' if state['configured'] else '未配置'}；来源：{state['source']}；"
                              f"本机加密凭据：{'存在' if state['stored_credential_present'] else '不存在'}。")
                    else:
                        removed = credentials.remove()
                        state = credentials.status()
                        print(f"已移除本机 DPAPI 凭据：{'是' if removed else '否'}；当前有效来源：{state['source']}。")
                elif line == "/model refresh":
                    client, source = _deepseek_client()
                    models = _registry().refresh_deepseek(client)
                    print(f"已从 DeepSeek 刷新 {len(models)} 个模型；密钥来源：{source}。")
                    for model in models:
                        print(f"{model.model_id}: context={model.context_window or 'unknown'}, "
                              f"max_output={model.max_output_tokens or 'unknown'}, "
                              f"thinking={model.supports_thinking if model.supports_thinking is not None else 'unknown'}, "
                              f"tools={model.supports_tool_calling if model.supports_tool_calling is not None else 'unknown'}")
                elif line.startswith("/model "):
                    current = _switch_model(line[len("/model "):].strip(), current)
                elif line == "/thinking on" or line == "/thinking off":
                    current = _update_thinking(current, thinking=line.endswith("on"))
                elif line.startswith("/effort "):
                    effort = line[len("/effort "):].strip()
                    if effort not in {"low", "high", "max"}:
                        raise ValueError("用法：/effort low|high|max")
                    current = _update_thinking(current, effort=effort)
                elif line == "/cloud-consent status":
                    if not current:
                        raise ValueError("请先选择或新建会话。")
                    consent = current.get("cloud_consent_at")
                    print("当前会话云端数据许可：" + ("已授权 " + str(consent) if consent else "未授权"))
                elif line == "/cloud-consent revoke":
                    if not current:
                        raise ValueError("请先选择或新建会话。")
                    result = _call("session", "revoke-cloud-consent", current["id"])["result"]
                    current = result
                    print("已撤销当前会话的云端数据许可；DeepSeek 请求将被阻止，模型绑定未自动切换。")
                elif line == "/stream on":
                    if not current or current.get("provider_id") != "deepseek":
                        raise ValueError("当前会话模型不支持已确认的 DeepSeek 流式输出。")
                    descriptor = _registry().get_for_request(current.get("model_id", ""))
                    stream_enabled = True
                    if descriptor.supports_streaming is True:
                        print("DeepSeek SSE 草稿显示已启用；草稿标为未验证，最终回答仍需通过引用校验。")
                    elif descriptor.supports_streaming is False:
                        print("该模型不支持原生 Streaming；仍会使用 DeepSeek 完整回答。")
                    else:
                        print("该模型的原生 Streaming 能力尚未确认；仍会使用 DeepSeek 完整回答。")
                elif line == "/stream off":
                    stream_enabled = False
                    print("DeepSeek SSE 草稿显示已关闭。")
                elif line == "/debug on":
                    debug_enabled = True
                    print("内部阶段详情已显示；诊断 JSONL 持续记录。")
                elif line == "/debug off":
                    debug_enabled = False
                    print("内部阶段详情已隐藏；诊断 JSONL 持续记录。")
                elif line == "/repo list":
                    repositories = _call("repo", "list")["repositories"]
                    for index, item in enumerate(repositories, 1):
                        marker = " *" if item.get("active") else ""
                        print(f"{index}. {item['display_name']} ({item['repository_id']}){marker}")
                elif line.startswith("/repo register "):
                    path = line[len("/repo register "):].strip().strip('"')
                    result = _call("repo", "register", path)
                    item = result["repository"]
                    print(f"已登记仓库 {item['display_name']} ({item['repository_id']})；索引状态："
                          f"{'已建立' if result['indexed'] else '未建立，请执行 /repo index ' + item['repository_id']}。")
                elif line.startswith("/repo switch "):
                    selector = line[len("/repo switch "):].strip()
                    repositories = _call("repo", "list")["repositories"]
                    if selector.isdigit():
                        number = int(selector)
                        if number < 1 or number > len(repositories):
                            raise ValueError("仓库编号无效。")
                        selector = repositories[number - 1]["repository_id"]
                    result = _call("repo", "switch", selector)["repository"]
                    repository = result["canonical_path"]
                    current = _call("session", "current")["result"]
                    suggestions = []
                    print("已切换仓库：" + repository)
                elif line == "/repo index" or line.startswith("/repo index ") or line == "/repo rebuild" or line.startswith("/repo rebuild "):
                    parts = line.split(maxsplit=2)
                    action = parts[1]
                    args = ["repo", action]
                    if len(parts) == 3:
                        args.append(parts[2].strip().strip('"'))
                    result = _call(*args)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                elif line.startswith("/repo remove "):
                    repository_id = line[len("/repo remove "):].strip()
                    if input("将移除该仓库的 AgentForge 索引和 Chroma 向量，不删除源代码；请先切换到其他已索引仓库。输入 DELETE 确认：").strip() == "DELETE":
                        _call("repo", "remove", repository_id, "--confirm")
                        current = None
                        print("仓库索引已移除；PostgreSQL 会话与记忆保留。")
                elif line.startswith("/new "):
                    made = _create_default_conversation(line[5:].strip())
                    if made is not None:
                        current = made
                        suggestions = []
                elif line.startswith("/switch "):
                    sessions = _call("session", "list")["result"]
                    number = int(line[8:].strip())
                    if number < 1 or number > len(sessions):
                        raise ValueError("会话编号无效。")
                    current = _call("session", "switch", sessions[number - 1]["id"])["result"]
                    suggestions = []
                elif line.startswith("/rename ") and current:
                    current = _call("session", "rename", current["id"], line[8:].strip())["result"]
                elif line == "/history" and current:
                    messages = _call("session", "show", current["id"])["result"]["messages"]
                    for message in messages:
                        print(f"{message['role']}: {message['content']}")
                elif line == "/chroma status":
                    result = _call("chroma", "status")
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                elif line in ("/chroma index", "/chroma rebuild"):
                    action = "rebuild" if line.endswith("rebuild") else "index"
                    result = _call("chroma", action)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                elif line == "/chroma on":
                    chroma_enabled = True
                    print("已启用 Chroma 向量检索；服务不可用时会自动退回 SQLite 检索。")
                elif line == "/chroma off":
                    chroma_enabled = False
                    print("Chroma 检索已关闭。")
                elif line == "/memory list":
                    result = _call("memory", "list")["result"]
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                elif line == "/memory global list":
                    result = _call("memory", "list", "--scope", "global")["result"]
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                elif line.startswith("/memory global approve "):
                    memory_id = line[len("/memory global approve "):].strip()
                    result = _call("memory", "approve", memory_id, "--scope", "global")["result"]
                    print(f"已批准全局记忆 {result['id']}。")
                elif line.startswith("/memory global revoke "):
                    memory_id = line[len("/memory global revoke "):].strip()
                    result = _call("memory", "revoke", memory_id, "--scope", "global")["result"]
                    print(f"已撤销全局记忆 {result['id']}。")
                elif line == "/memory suggest" and current:
                    result = _call("memory", "suggest", current["id"],
                        "--base-url", "http://127.0.0.1:56572/v1/chat/completions",
                        "--model", "qwen3.5-4b")["result"]
                    suggestions = result["candidates"]
                    for index, item in enumerate(suggestions, 1):
                        print(f"{index}. [{item['memory_type']}] {item['content']} "
                              f"(来源消息序号: {item['source_sequences']})")
                    print("这些只是模型建议；输入 /memory save 编号 后才会保存为待审批项。")
                elif line.startswith("/memory save ") and current:
                    number = int(line[len("/memory save "):].strip())
                    if number < 1 or number > len(suggestions):
                        raise ValueError("记忆建议编号无效；请先运行 /memory suggest。")
                    item = suggestions[number - 1]
                    args = ["memory", "propose", current["id"], item["content"], "--type", item["memory_type"]]
                    for message_id in item["source_message_ids"]:
                        args.extend(["--source-message-id", message_id])
                    saved = _call(*args)["result"]
                    print(f"已保存为待审批记忆 {saved['id']}；执行 /memory approve {saved['id']} 才会生效。")
                elif line.startswith("/memory approve "):
                    memory_id = line[len("/memory approve "):].strip()
                    result = _call("memory", "approve", memory_id)["result"]
                    print(f"已批准记忆 {result['id']}。向量待同步；Embedding 服务可用时运行 /memory sync。")
                elif line.startswith("/memory reject "):
                    memory_id = line[len("/memory reject "):].strip()
                    print(json.dumps(_call("memory", "reject", memory_id)["result"], ensure_ascii=False))
                elif line.startswith("/memory revoke "):
                    memory_id = line[len("/memory revoke "):].strip()
                    print(json.dumps(_call("memory", "revoke", memory_id)["result"], ensure_ascii=False))
                elif line.startswith("/memory update "):
                    parts = line.split(" ", 3)
                    if len(parts) != 4 or not parts[2] or not parts[3].strip():
                        raise ValueError("用法：/memory update 记忆ID 新内容")
                    print(json.dumps(_call("memory", "update", parts[2], parts[3])["result"],
                                       ensure_ascii=False, indent=2))
                elif line.startswith("/memory show "):
                    memory_id = line[len("/memory show "):].strip()
                    print(json.dumps(_call("memory", "show", memory_id)["result"],
                                       ensure_ascii=False, indent=2))
                elif line == "/memory sync":
                    print(json.dumps(_call("memory", "sync")["result"], ensure_ascii=False, indent=2))
                elif line == "/memory rebuild":
                    print(json.dumps(_call("memory", "sync", "--rebuild")["result"], ensure_ascii=False, indent=2))
                elif line == "/memory status":
                    print(json.dumps(_call("memory", "status")["result"], ensure_ascii=False, indent=2))
                elif line == "/memory expire":
                    print(json.dumps(_call("memory", "expire")["result"], ensure_ascii=False, indent=2))
                elif line == "/delete" and current:
                    if input("确认删除当前会话及全部历史？输入 DELETE 确认：").strip() == "DELETE":
                        _call("session", "delete", current["id"], "--confirm")
                        current = None
                elif line.startswith("/"):
                    print("命令无效，或尚未选择会话。")
                elif not current:
                    print("请先输入 /new 标题 新建会话，或用 /list 和 /switch 编号切换会话。")
                else:
                    args = ["ask", line, "--conversation-id", current["id"],
                        "--read-timeout", "120", "--mode", dialogue_mode,
                        "--output-budget", output_budget_mode]
                    if current.get("provider_id") == "deepseek":
                        args.extend(["--provider", "deepseek", "--model", current["model_id"],
                                     "--thinking", "on" if current.get("thinking_enabled") else "off"])
                        if current.get("reasoning_effort"):
                            args.extend(["--effort", current["reasoning_effort"]])
                        if stream_enabled:
                            args.append("--stream")
                    elif current.get("provider_id") == "llama-server":
                        args.extend(["--provider", "llama-server", "--base-url", _QWEN_ENDPOINT,
                                     "--model", current.get("model_id", "qwen3.5-4b")])
                    else:
                        args.extend(["--provider", "offline"])
                    args.append("--use-chroma" if chroma_enabled else "--no-chroma")
                    if debug_enabled:
                        args.append("--debug")
                    presentation.main(args)
            except (ValueError, KeyError, AgentForgeError, OSError, RuntimeError) as exc:
                print(f"操作失败：{exc}")
        return 0
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        if str(exc):
            print(f"会话启动失败：{exc}", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
