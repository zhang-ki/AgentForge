# AgentForge V1 测试清理记录

2026-10-27。测试均以 `-B` 执行，未创建覆盖率产物或模型运行时。`LocalTestCase` 在项目内创建独立 `tests/.tmp/case-*` 临时目录并自动删除；最终检查没有该类遗留，也没有 `af011-chat-*` 目录及 llama-server 进程。

| 精确路径或类别 | 决定 | 依据 |
|---|---|---|
| `tests/.tmp/af009c-llama-server` | 保留 | 本轮前已存在、检查为空且被多份历史报告引用；用户数据不可凭空判断为本轮垃圾 |
| `D:/agent/AgentForge/__pycache__/` | 保留 | 本轮前已存在，包含历史编译产物；未证明本轮创建 |
| `D:/agent/AgentForge/tests/__pycache__/` | 保留 | 本轮前已存在，未证明本轮创建 |
| `tests/fixtures/` | 保留 | 冻结的合成仓库和评测证据 |
| 正式索引、只读证据、历史 JSON、报告、模型及运行时 | 保留 | 受保护数据或历史验收记录 |
| `.coverage`、`htmlcov/` | 未创建 | 现有覆盖率工具不可用 |

本轮没有发现需要人工删除的本轮自有残留，因此没有删除任何既有文件或目录。测试夹具自动清理仅限其创建的临时目录。未终止任何进程；最终未发现 llama-server 进程。

## 后续覆盖率测量

本轮用户授权后在项目内独立环境安装 coverage.py 7.17.0。第一次环境位于项目根目录导致完整扫描达到文件上限，已将该本轮创建的环境移入扫描器排除的 `tests/.tmp/.venv`；重测后完整覆盖率测试通过。覆盖率机器报告已保存为 `TEST_COVERAGE_RESULTS.json`。

- 已删除的精确目录：`D:\agent\AgentForge\tests\.tmp\.venv`，包括本轮安装的隔离工具和本轮原始 coverage 数据。删除前核实其为本轮创建的普通目录、目标路径位于工作区、没有 Python 进程使用，且 JSON 与可读报告均已保存；删除后确认目录不存在。
- 保留：`tests/.tmp/af009c-llama-server`、原有缓存、正式索引、历史结果及 `TEST_COVERAGE_RESULTS.json`。
- 最初 ensurepip 曾在受限环境默认系统临时位置创建临时目录并因权限报错，无法在项目内核实或清理该位置；随后已将临时路径指向本轮隔离环境并完成安装。没有修改系统权限。

## 硬链接专项清理

- 删除本轮创建的 `D:\agent\AgentForge\tests\.tmp\hardlink-probe-3sw86kg1`。探针自身在 Windows 上因打开的句柄尚未关闭而未能自动删除；进程退出后核对该目录只有本轮的两个 5 字节合成文件，再删除并确认路径不存在。
- 已删除本轮创建的 `D:\agent\AgentForge\tests\.tmp\.venv`，其中包括隔离的 coverage.py 7.17.0 和临时 coverage 数据；删除前核对目标路径、无 Python 进程、当前及旧版 JSON 报告已保存，删除后确认目录不存在。
- 保留 `TEST_COVERAGE_RESULTS_PRE_HARDLINK.json`，其 SHA-256 为 `CCECA12937AF67B711E4769DA87EF50D3257D3D90A876B14BB8D7321572312B0`，以保留修复前覆盖率证据。既有空 `tests/.tmp/af009c-llama-server` 和历史缓存未动。

## 2026-10-27 最终覆盖率任务
本轮建立的 `tests/.tmp/.venv` 已在保存 JSON、完整测试日志及原始覆盖数据 `coverage/TEST_COVERAGE_FINAL.data` 后，核实绝对路径与非重解析点并删除。预存 `tests/.tmp/af009c-llama-server` 原样保留。未启动模型、未分配模型端口；结束时未发现 llama-server 进程。没有删除历史报告、证据或用途不明的目录。

## 最终宿主确认后的资源检查（2026-10-27）
本次未启动模型、未创建临时运行时或测试环境；进程检查未发现 llama-server。`tests/.tmp/.venv` 不存在，既有 `tests/.tmp/af009c-llama-server` 保留。本次未删除任何文件或目录。完整性清单保存在 `coverage/TEST_FINAL_INTEGRITY_20261027.json`，避免将大型报告放在扫描范围的根目录。
