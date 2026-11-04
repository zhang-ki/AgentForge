# AF-012 1.2 多模型支持验收报告

> 本报告保留原阶段快照。后续 Thinking 输出预算修复及新的复验状态见 `AF012_THINKING_BUDGET_FIX_REPORT_20261104.md`；原文中的每次 48 token 上限描述对应修复前版本。

日期：2026-11-04
分支：`feature/1.1-core`
状态：**PARTIAL**

## 结论

Model Registry、DeepSeek Provider、会话级模型设置、凭据管理、命令入口和动态上下文预算已完成离线实现及自动化回归。当前没有证据证明 DeepSeek 或本轮代码变更后的 Qwen 已通过真实宿主验收：宿主 Runner 检查到 `CODEX_SANDBOX_NETWORK_DISABLED=1` 后在环境门禁停止，真实模型请求为 0。

此外，全 `agentforge` 覆盖率为 87.20% 语句覆盖率和 79.49% 分支覆盖率，未达到全包 90% / 85% 目标。新增加的 Registry、DPAPI 凭据逻辑、Token Budget 和 DeepSeek Provider 模块各自达到 100% 行与分支覆盖，但这不能替代全包门槛或真实模型验收。

## 已实现并通过离线检查

- Qwen Local 仍是新会话默认 Provider；Offline Provider 保留。
- DeepSeek 模型注册信息可从 `/models` 响应解析并缓存；连接状态与模型元数据分开处理，未知能力保持未知。
- DeepSeek Chat Completions 使用固定 HTTPS 主机、Bearer 凭据、明确超时与单次请求策略；reasoning 与最终 `content` 分开处理，支持流式响应和用量统计。
- DeepSeek 工具规划仅映射到现有只读搜索与符号工具，不提供 shell、任意 Python、写入或网络工具。
- 会话绑定 Provider、Model ID、Thinking、effort 和云端数据同意信息使用追加式迁移 `af012mm_004`。本地 Alembic 图的 head 已检查为 `af012mm_004`；正式 PostgreSQL 是否已应用该迁移未测量。
- 凭据实现支持当前 Windows 用户 DPAPI 存储和 `DEEPSEEK_API_KEY` 覆盖；状态命令只显示凭据来源。没有把密钥写入源码、结果或日志。本轮宿主门禁先于读取凭据，因此本机凭据状态未测量。
- Token Budget 根据模型元数据限制输入和输出预算，并计入消息协议、工具 Schema 等内容。当前没有可用的模型对应 tokenizer 时会明确使用 `estimated` 估算；不能称为精确 token 计数。
- 修复了 `/model auth status` 命令参数校验，以及成功模型发现请求被同时记为失败的统计问题，均有离线回归测试。

## 离线测试

使用项目虚拟环境 Python 3.12.7、coverage.py 7.17.0。完整测试命令为：

```powershell
.agentforge\af012-venv\Scripts\python.exe -X utf8 -B -m coverage run --data-file=coverage\AF012_MULTIMODEL_FINAL_20261104.coverage --branch --source=agentforge -m unittest discover -s tests -v
```

结果：520 项运行，514 项通过、0 失败、0 错误、6 项跳过，退出码 0。6 项跳过均受 Windows 硬链接、符号链接或 junction 的权限限制，不计入通过数。完整输出见 `coverage/AF012_MULTIMODEL_FINAL_FULL_TESTS_20261104.log`。

多模型、会话命令、CLI 和宿主 Runner 的核心测试分别以两种模块顺序执行：正序 80/80、逆序 80/80，均退出码 0。日志见 `coverage/AF012_MULTIMODEL_CORE_FORWARD_20261104.log` 与 `coverage/AF012_MULTIMODEL_CORE_REVERSE_20261104.log`。CLI 帮助检查退出码为 0。

### 覆盖率

测量范围为全部 65 个 `agentforge` 生产文件，启用分支覆盖率，没有新增排除规则：

| 范围 | 行/语句覆盖率 | 分支覆盖率 |
|---|---:|---:|
| 全部生产包 | 87.195% | 79.486% |
| `model_registry.py` | 100% | 100% |
| `credential_store.py` | 100% | 100% |
| `token_budget.py` | 100% | 100% |
| `providers/deepseek.py` | 100% | 100% |
| `providers/deepseek_agent.py` | 100% | 100% |
| `providers/llama_server.py` | 95.71% | 80.85% |

全包目标未达到；尤其既有 Qwen Provider 分支覆盖率仍低于核心模块 90% 门槛。机器数据见 `coverage/AF012_MULTIMODEL_FINAL_RESULTS_20261104.json`，文本报告见 `coverage/AF012_MULTIMODEL_FINAL_COVERAGE_REPORT_20261104.log`。

## 真实宿主结果

Runner：`AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1`
原始结果：`AF012_MULTIMODEL_HOST_RESULT_b20b34b0582247a3b6147074844444df.json`

原始机器结果为 `BLOCKED`，阶段 `environment`，原因 `network_disabled_execution_environment`。Runner 没有探测网络、读取凭据、连接 PostgreSQL/Chroma/Qwen 或启动/停止服务。DeepSeek discovery 请求 0 次；聊天 attempted/sent/succeeded/failed/retries 均为 0。

因此，下列项目本轮均为 `NOT_MEASURED`：真实 DeepSeek `/models` 返回及可用模型、真实 DeepSeek 问答/Thinking/流式/tool calling、改动后 Qwen 问答、正式数据库迁移与会话绑定恢复、11 项真实 RAG 验收、30 轮真实上下文、桌面关闭重启、真实 usage 及费用。离线测试使用模拟响应和合成数据，不作为这些项目的真实通过证据。

Runner 是有界 DeepSeek 合成内容 Smoke Test，不是完整 1.2 联合验收器。普通 Windows PowerShell 可在安全配置新 Key 后运行：

```powershell
cd D:\agent\AgentForge
powershell -NoProfile -File "D:\agent\AgentForge\AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1"
```

它会先显示云端数据提示，再要求明确输入 `SEND SYNTHETIC TESTS`；最多发现 2 个模型、发送最多 6 个聊天请求、每次最多 48 个输出 token，不自动重试。不要复用此前已暴露的 Key；在本机通过 `/model auth set deepseek` 安全设置新 Key。该 Smoke Test 通过也不等同于完整 1.2 验收。

## 数据、Git 与剩余门槛

- 本轮真实宿主结果表明没有访问或修改正式 PostgreSQL、Chroma 或服务；全套离线测试使用合成/隔离数据。没有对正式用户数据做本轮大小和 SHA-256 基线审计，因此不宣称完成正式数据哈希核验。
- 工作区原本存在未提交修改；已保留，当前分支仍为 `feature/1.1-core`。没有切换分支、提交或推送；`main` 与 `v1.0.0-mvp` 未触碰。
- 无已确认的离线 P0/P1 代码缺陷仍未修复。交付阻塞是：真实宿主验收未执行、覆盖率门槛未达到、正式迁移和模型能力未验证。

## 阶段判定

**PARTIAL**。离线实现和自动化测试通过，但不能称为 1.2 完整发布。真实 Qwen/DeepSeek 与 PostgreSQL 会话绑定复验，以及全包覆盖率差距仍需处理。
