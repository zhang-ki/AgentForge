"""Explicit interactive multi-session entry point; no model service management."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys

from . import cli, presentation


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
        chroma_enabled = os.environ.get("AGENTFORGE_CHROMA_ENABLED") == "1"
        repository = config["repository"]
        print("当前仓库：" + repository)
        print("命令：/new 标题、/list、/switch 编号、/rename 标题、/history、/delete、"
              "/repo list|register 路径|switch 编号或ID|index [ID或路径]|rebuild [ID或路径]|remove ID、"
              "/memory list|suggest|save|approve|update|revoke|sync|rebuild、/memory global list|approve ID|revoke ID、"
              "/chroma status|index|rebuild|on|off、/exit")
        while True:
            config = _call("session", "current")
            if config["repository"] != repository:
                repository = config["repository"]
                print("索引仓库已切换：" + repository)
            current = config["result"]
            print("\n当前会话：" + (current["title"] if current else "未选择"))
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
                    made = _call("session", "create", line[5:].strip())["result"]
                    current = _call("session", "switch", made["id"])["result"]
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
                    required = ("AGENTFORGE_EMBEDDING_BASE_URL", "AGENTFORGE_EMBEDDING_MODEL_ID",
                                "AGENTFORGE_EMBEDDING_API_MODEL")
                    if not all(os.environ.get(name) for name in required):
                        raise ValueError("请先配置独立的本地 Embedding 服务环境变量，再启用 Chroma。")
                    chroma_enabled = True
                    print("Chroma 代码与已批准记忆检索已启用。")
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
                        "--provider", "llama-server", "--base-url", "http://127.0.0.1:56572/v1/chat/completions",
                        "--model", "qwen3.5-4b", "--read-timeout", "120"]
                    if chroma_enabled:
                        args.append("--use-chroma")
                    presentation.main(args)
            except (ValueError, KeyError) as exc:
                print(f"操作失败：{exc}")
        return 0
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        if str(exc):
            print(f"会话启动失败：{exc}", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
