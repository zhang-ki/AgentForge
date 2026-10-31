# AF-010-S5-FINAL：最终真实 Vector 联合验收

日期：2026-10-26  
最终状态：**`blocked_by_environment`**

## 执行结论

本会话是 Windows（Python 3.12.7），但当前进程环境设置 `CODEX_SANDBOX_NETWORK_DISABLED=1`，且没有另一个平台正式支持、允许 loopback 的宿主执行入口。因此按门禁停止真实模型部分：没有重复 TCP 自检，没有读取模型资产或计算模型/运行时哈希，没有创建运行时副本、启动服务、请求 `/health` 或发送 Embedding。真实 Vector 与包含 Vector 的完整 RRF 均未测量。历史 `AF010S5_RESULTS.json` 的离线评测被复用，没有重跑离线评测。

这是本轮唯一的**环境执行阻塞**。任务中点名的 `AF009GR5_HOST_RUNNER.py` 文件名在当前目录不存在；审查发现 `AF009GR3_HOST_RUNNER.py` 的模块说明为 AF-009G-R5 one-shot host runner，`AF010S2_HOST_RUNNER.py` 复用其模型 readiness 状态机和资源清理逻辑。Runner 能力并非当前环境门禁的阻塞原因。

## Runner 与安全边界审查

- `AF009GR3_HOST_RUNNER.py` 的顺序包含进程启动、TCP readiness、模型加载状态和严格 `/health` readiness；仅在合法 HTTP 200 且 JSON `status == "ok"` 后调用 Embedding 评测。加载期 503 只接受指定的 llama.cpp Loading model 错误结构。
- 该 Runner 使用直接回环、无代理且不跟随重定向的 Health 请求，设定请求与总等待上限，启动前核验固定模型及 EXE/DLL 哈希、复制运行时并校验副本，重定向 stdout/stderr 到内存并清理自有进程。
- `AF010S2_HOST_RUNNER.py` 针对冻结 S2 评测集调用 `evaluate_s2()`，并使用共享 readiness 状态机。代码审查未发现需要为了本轮环境阻塞而重写 Runner 的理由。两个 Runner 本轮均未执行。
- 当前环境不允许执行真实验收；因此模型资产哈希、模型 readiness、服务退出码、Embedding 数量与耗时均为 `not_measured`，不是通过。

## 离线结果（复用，不重算）

数据来自冻结的 `agentforge-package-relations-multirepo-v1`：3 个合成仓库、120 条查询；Calibration/Validation 各 60 条，各含 30 正例和 30 无证据查询。leakage group 隔离通过，标签来源为合成 fixture 设计且 `human_reviewed=false`。

| Split | 通道 | 适用正例 | Recall@1 / @5 / @10 | MRR | 无证据非空 |
|---|---|---:|---:|---:|---:|
| Calibration | Lexical | 30 | 0.6667 / 1 / 1 | 0.8167 | 30/30 |
| Calibration | Symbol | 6 | 1 / 1 / 1 | 1 | 0/6（仅适用子集） |
| Calibration | PackageRelation | 15 | 0.5333 / 1 / 1 | 0.7667 | 15/15 |
| Calibration | 离线 RRF（三通道） | 30 | 0.6333 / 1 / 1 | 0.8056 | 30/30 |
| Calibration | 路由 | 30 | 0.6667 / 1 / 1 | 0.8222 | 30/30 |
| Validation | Lexical | 30 | 0.8000 / 1 / 1 | 0.8778 | 30/30 |
| Validation | Symbol | 6 | 1 / 1 / 1 | 1 | 0/3（仅适用子集） |
| Validation | PackageRelation | 18 | 0.8889 / 1 / 1 | 0.9444 | 21/21 |
| Validation | 离线 RRF（三通道） | 30 | 0.7000 / 0.9000 / 1 | 0.8044 | 30/30 |
| Validation | 路由 | 30 | 0.8333 / 1 / 1 | 0.9167 | 30/30 |

Symbol 和 PackageRelation 只在其定义的适用查询子集上计分；离线 RRF 只融合 Lexical、Symbol、PackageRelation，不是完整 Vector+RRF。各可测通道仍对绝大多数适用无证据查询返回候选，不能据此声称具备拒答能力。由于没有 Vector cosine 分数，固定 `top_score >= 0.70` 的覆盖率、误接受率、误拒绝率和精确率均为 `not_measured`；没有将其它通道分数代替余弦相似度。

历史路由版本字符串仍为 `package-router-calibration-v1`，当前实现相对旧 S2 结果改变了 3 条 Calibration 动态导入查询的路由决定。差异保留在 S5 结果中；本轮没有使用 Validation 调参。

## 测试与完整性

执行命令：

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

结果：181 项运行，176 通过、0 失败、0 错误、5 跳过，退出码 0。跳过的 5 项因 Windows 权限不足，无法创建符号链接、junction 或硬链接；没有计入通过数。该套件中的 readiness、HTTPError/503、状态机和进程清理均为 Mock/模拟测试，不代表真实模型通过。

核对的 169 个受保护路径均存在，大小和 SHA-256 与 S5 初始快照及其运行后快照一致。正式索引为 32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`；只读证据文件为 2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。冻结评测 fixture、历史结果和 split plan 均包含在受保护集合中并核验未变化。没有访问或修改 Agentexpr。

本轮没有启动模型进程、分配端口或创建运行时副本；因此没有本轮自有进程需要终止，也没有服务临时目录需要清理。既有 `tests/.tmp/af009c-llama-server` 保持未触碰。

## 交付与决定

- 新增 `AF010S5_FINAL_RESULTS.json` 与本报告；保留原有 S5 结果与报告。
- 更新 `STATUS.md`、`ROADMAP.md`、`FILE_MANIFEST.md` 的当前阶段记录。
- 最终状态：**`blocked_by_environment`**。真实 Vector/完整 RRF/固定余弦拒答规则未验收，不满足完整真实联合验收条件。
- 默认 Retriever、RRF 产品配置和生产拒答阈值未改变；没有实施 reranker，也没有扩大项目功能。

继续验收的唯一环境前置条件是：在平台正式支持且允许本机 loopback 的 Windows 宿主执行上下文中运行已审查的 S2 Host Runner，先核对固定资产哈希并通过模型级 Health gate，再进行一次冻结 Embedding 评测。不得在当前受限上下文中探测或规避限制。
