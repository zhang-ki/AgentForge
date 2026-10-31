# AgentForge V1 覆盖率报告

2026-10-27：`NOT_MEASURED`，覆盖率验收未通过。当前 Python 3.12.7 的 `python -m coverage --version` 报 `No module named coverage`；`python -m pip show coverage` 显示未安装，`coverage` 和 `coverage3` 命令均未找到。没有安装、联网或引入替代依赖。

| 范围 | 行覆盖率 | 分支覆盖率 | 目标 |
|---|---|---|---|
| 全部生产代码 `agentforge/` | `NOT_MEASURED` | `NOT_MEASURED` | ≥90% / ≥85% |
| Provider、引用、Answer Contract、Agent Loop | `NOT_MEASURED` | `NOT_MEASURED` | ≥95% / ≥90% |

没有覆盖工具，因此无法提供未覆盖行号或分支，也不能从通过的测试数推导覆盖率。应在未来获得本地可用且经批准的 coverage.py 后，以生产目录为 source、开启 branch，单独导出缺失行和分支，再优先补安全拒绝和异常恢复。建议命令（当前环境不可执行）：

```powershell
python -m coverage run --branch --source=agentforge -m unittest discover -s tests -v
python -m coverage report -m
```

覆盖率数据文件应放在专用测试目录，先确认不会覆盖已有文件。不得将 `tests/` 或生成代码并入生产覆盖率；不得删除防御性代码或将系统专属分支大面积排除。5 个 Windows 链接/junction/硬链接测试因权限不足跳过，系统实现本身仍需有适当权限的独立环境验证。未运行变异测试；没有已验证的变异工具。

## 2026-10-27 后续真实测量

上述 `NOT_MEASURED` 是此前历史状态，保留供追溯。此前用户授权后，使用官方 PyPI 的 coverage.py 7.17.0，在项目专用 `.venv` 中测量 `--branch --source=agentforge`，没有修改项目依赖或全局解释器。该批次原始机器报告保留为 `TEST_COVERAGE_RESULTS_PRE_HARDLINK.json`；硬链接修复批次的报告保留为 `coverage/TEST_COVERAGE_RESULTS_BEFORE_FINAL.json`。覆盖统计不包含 `tests/`；不额外增加排除规则。coverage.py 报告的 3 行原有排除仍保留。

| 范围 | 行覆盖率（命中/可执行） | 分支覆盖率（命中/总数） | 门槛 |
|---|---:|---:|---|
| 全部 `agentforge` | 86.83%（4352/5012） | 79.36%（1473/1856） | 90% / 85%，未达到 |
| `providers/llama_server.py` | 98.40%（185/188） | 92.86%（52/56） | 95% / 90%，达到 |
| `context.py` | 98.90%（90/91） | 97.50%（39/40） | 达到 |
| `answer_contract.py` | 98.04%（100/102） | 94.74%（36/38） | 达到 |
| `agent_loop.py` | 97.93%（237/242） | 94.12%（64/68） | 达到 |
| `cli.py` | 97.60%（163/167） | 90.91%（40/44） | 达到 |
| `embeddings/llama_server.py` | 88.33%（106/120） | 75.00%（27/36） | Embedding Provider 未达到核心门槛 |
| `embeddings/base.py` | 83.33%（60/72） | 78.57%（22/28） | Embedding 接口仍有缺口 |
| `providers/base.py` | 0%（0/4） | 无分支（0/0） | 抽象接口声明未由本次进程执行；0/0 不代表验收通过 |
| `product_retrieval.py` | 87.70%（271/309） | 83.00%（83/100） | 全包缺口之一 |
| `scanner.py` | 83.06%（206/248） | 83.70%（77/92） | 安全路径仍有缺口 |
| `safeio.py` | 68.42%（39/57） | 44.44%（8/18） | 平台及错误路径仍有缺口 |

这里的行覆盖率是 `covered_lines / num_statements`，分支覆盖率是 `covered_branches / num_branches`；`coverage report` 的 Cover 列是语句和分支的合并值，不应混用。首次有效基线分别为 85.71%（4296/5012）和 76.94%（1428/1856）。

本轮新增 `tests/test_quality_coverage.py`，以隔离数据、模拟 HTTP 和真实测试临时文件覆盖异常响应结构、引用合同拒绝、非法参数和时间预算、向量候选的真实来源一致性、扫描不完整及文件身份/读取中元数据变化。没有发现需修改产品代码的可复现缺陷。模拟文件身份不能代替真实 NTFS 链接行为验证。

主要未覆盖项及影响：

- `safeio.py:10-17` 为非 Windows 分支，本机 Windows 不能真实执行；42、52、54、58、61、65、70–72、75 为 Windows 句柄失败和清理边界，尚需可控模拟或对应 OS 环境。真实符号链接、junction、硬链接仍有 5 项因权限跳过。
- `scanner.py:99-100,109,113,119,121-124,172,177-180,201-205,212-215,219-221,227-229,238-245,294-301,314` 涉及路径解析、文件系统异常、扫描中变化及大小/类型拒绝；本轮已补模拟身份替换和读取中修改，但不等于其余分支已验证。
- `package_relations.py:129-147,152-185` 和 `package_s2.py:418-467` 为旧快照路径及规模测量等未执行区域；`eval_generalization.py:3-81`、`eval_provisional.py:3-164`、`eval_retrieval.py:3-50` 的入口未在本次测试进程执行。不能把这些缺口全部归因于平台，需要后续隔离测试。
- `product_retrieval.py:112,116,131,134-135,147-149,168,186,195,210-215,234-236,241,260-267,303-308,326,366,390-393,408,449` 仍含索引不可用、静态关系异常与回退分支；`embeddings/vector_store.py:22,33,72,83,94,97,101-102,126,128,139,155,157,179,188,196-200,205,230,234-235,260` 仍含数据验证和事务边界。应优先补可复现的失败恢复用例。
- Embedding Provider 的 `llama_server.py:41,64,73-74,82,109,138-142,145,164-165` 及 `base.py:42,55,58-59,63,71,74-75,81-82,84,88` 尚未覆盖，错误解析、输入限制和接口异常应继续补测；不能将聊天 Provider 达标推广为所有 Provider 达标。
- 聊天核心模块剩余未覆盖分支详见机器报告 `missing_branches`；它们达到数值目标不意味着所有拒绝路径均已实测。

最终覆盖率验收 `PARTIAL`，而非 PASS：全包仍缺 159 条已覆盖语句和 105 个已覆盖分支才能达到目标（按当前分母向上取整）；缺口中既有系统限制，也有尚待测试的常规代码。本轮最终覆盖测试 232 项，227 通过、0 失败、0 错误、5 跳过，退出码 0。

可复现（本轮隔离环境已清理，需先在隔离环境中重新获得已授权的 coverage.py；以下命令会生成并覆盖指定的覆盖率报告）：

```powershell
$env:COVERAGE_FILE = 'tests/.tmp/.venv/coverage.data'
./tests/.tmp/.venv/Scripts/python.exe -X utf8 -B -m coverage erase
./tests/.tmp/.venv/Scripts/python.exe -X utf8 -B -m coverage run --branch --source=agentforge -m unittest discover -s tests -v
./tests/.tmp/.venv/Scripts/python.exe -X utf8 -B -m coverage report -m
./tests/.tmp/.venv/Scripts/python.exe -X utf8 -B -m coverage json -o TEST_COVERAGE_RESULTS.json
```

## 硬链接修复后的最终测量

普通 Windows 宿主、Python 3.12.7、coverage.py 7.17.0；完整覆盖率测试 238 项运行、235 通过、0 失败、0 错误、3 项符号链接权限跳过，退出码 0。真实硬链接与 junction 用例在该宿主实际通过。此前受限环境中硬链接创建被拒绝，产生 5 项跳过；两个批次的跳过数不能混用。

| 范围 | 行覆盖率 | 分支覆盖率 |
|---|---:|---:|
| 全部 `agentforge` | 87.10%（4382/5031） | 79.83%（1488/1864） |
| 聊天 Provider | 98.40%（185/188） | 92.86%（52/56） |
| Embedding Provider | 99.17%（120/121） | 91.67%（33/36） |
| `context.py` | 98.90%（90/91） | 97.50%（39/40） |
| `answer_contract.py` | 98.04%（100/102） | 94.74%（36/38） |
| `agent_loop.py` | 97.93%（237/242） | 94.12%（64/68） |
| `scanner.py` | 82.71%（220/266） | 85.00%（85/100） |
| `safeio.py` | 71.93%（41/57） | 50.00%（9/18） |
| `embeddings/vector_store.py` | 84.67%（127/150） | 68.00%（34/50） |
| `package_relations.py` | 88.06%（531/603） | 82.48%（193/234） |

`agentforge/eval_generalization.py`（3–81）、`eval_provisional.py`（3–164）和 `eval_retrieval.py`（3–50）未在本次测试进程执行；`scanner.py` 未覆盖行包括 101–102、111、115、121、123–126、179–180、203–207、214–217、221–223、229–231、240–241、246–257、262–263、314–319、325 和 340；`safeio.py:10-17` 为非 Windows 路径，42、52、54、58、61、70–72 等为句柄错误/清理路径。全部准确的缺失行和分支见 `TEST_COVERAGE_RESULTS.json`。未覆盖项既包括平台限制，也包括尚未补足的普通回归；不得把所有缺口归因于平台。

真实硬链接缺陷的直接证据：Windows `DirEntry.stat(follow_symlinks=False)` 对测试硬链接返回 `st_nlink=0, st_dev=0, st_ino=0`，而路径 `lstat` 和已打开文件的 `fstat` 返回 `st_nlink=2` 及有效文件身份。修复后扫描阶段重新查询规范路径元数据；无法验证的身份被拒绝，读取时与扫描时身份及打开句柄再核对，并在读后复核链接数与身份。普通文件、扫描前硬链接、扫描后添加硬链接及扫描后替换普通文件均有回归。

另外，Embedding 响应的超大 `Content-Length` 原先因 `EmbeddingError` 继承 `ValueError` 而被误分类为格式无效；已先以 Mock 测试复现，再将整数转换异常与大小限制异常分开。没有真实网络请求。

质量门禁仍为 `PARTIAL`：虽然所列核心聊天/Embedding Provider、引用、回答合同和 Agent Loop 达到各自数值目标，生产包未达到 90% 行覆盖和 85% 分支覆盖，`safeio` 等关键文件仍有未覆盖拒绝路径。

## 2026-10-27 最终生产包覆盖率复测

质量门禁 **PARTIAL**：数值覆盖率和离线回归达到目标，但真实 Windows 文件、目录及替换符号链接三个安全测试因 WinError 1314 无法创建符号链接而跳过；模拟测试不替代真实操作系统验证。具备创建符号链接权限的普通 Windows 用户环境应独立重跑 `python -X utf8 -B -m unittest tests.test_scanner -v`，核实三项及其安全断言；不建议改变系统安全设置以制造通过。

使用隔离的 Python 3.12.7 与 coverage.py 7.17.0，`--branch --source=agentforge`，完整生产包 5031 条可执行语句、1864 个分支，保留原有 3 行排除、没有新增排除。行覆盖 **4599/5031 = 91.41%**，分支覆盖 **1603/1864 = 86.00%**。测试文件未计入；覆盖数据是测试主进程的测量，不能推断未纳入的子进程覆盖情况。旧批次结果已保留为 `coverage/TEST_COVERAGE_RESULTS_BEFORE_FINAL.json`，最终逐行与逐分支 JSON 为 `TEST_COVERAGE_RESULTS.json`，原始数据为 `coverage/TEST_COVERAGE_FINAL.data`，完整输出为 `coverage/TEST_COVERAGE_FINAL_RERUN_LOG.txt`。

| 模块 | 行 | 分支 |
|---|---:|---:|
| scanner | 96.99% (258/266) | 97.00% (97/100) |
| safeio | 98.25% (56/57) | 94.44% (17/18) |
| 聊天 Provider | 98.40% (185/188) | 92.86% (52/56) |
| Embedding Provider | 99.17% (120/121) | 91.67% (33/36) |
| context | 98.90% (90/91) | 97.50% (39/40) |
| Answer Contract | 98.04% (100/102) | 94.74% (36/38) |
| Agent Loop | 97.93% (237/242) | 94.12% (64/68) |
| CLI | 97.60% (163/167) | 90.91% (40/44) |
| 向量存储 | 97.33% (146/150) | 96.00% (48/50) |
| PackageRelation | 96.02% (579/603) | 90.17% (211/234) |

新增 `tests/test_final_coverage.py` 的 27 项覆盖 Windows 句柄异常与资源释放（Mock）、扫描身份变化及中断、真实临时仓库解析与缓存、内存 SQLite 向量及错误处理、证据与评测输入校验。真实硬链接及扫描后硬链接变化、Windows junction 均通过；符号链接的模拟覆盖不是实际系统验证。未覆盖例子：safeio 第 61 行 UNC 路径分支；scanner 179–182、214–217、340 行；Embedding Provider 82 行及 129→131、142→144；关系图及各评测入口的其余缺失行、分支以 JSON 为准，不能将全包达标解释为每条防御路径均已执行。

最终覆盖率回归和独立普通回归均 **265 项运行、262 通过、0 失败、0 错误、3 跳过、退出码 0**；新增定向 27/27 通过；核心正序与逆序各 80 项运行、77 通过、3 跳过、退出码 0。此前首次覆盖率全量运行因本轮在根目录生成大体积 JSON 使扫描达到字节上限而报 1 个错误；将本轮输出移至扫描器既有排除的 `coverage/` 后重跑通过，原始失败日志保留在 `coverage/TEST_COVERAGE_FINAL_RUN_LOG.txt`。没有修改扫描配置或生产安全边界。

## 用户提供的 Windows 宿主最终回归（2026-10-27）

最终质量门禁 **PASS（包含用户宿主测试确认）**。用户确认在具备符号链接权限的普通 Windows PowerShell 中，`tests.test_scanner` 16 项通过、无跳过；完整回归 265 项通过、无失败、无错误、无跳过，退出码 0。此前跳过的文件链接、目录链接和扫描后链接替换三个真实 OS 场景均已执行并通过。这些是用户提供的执行证据，本次未重跑，也没有在工作区找到该次原始输出日志；不能将其描述为本次代理直接观测的日志。

覆盖率仍为此前 `coverage.py 7.17.0` 的实测：行 4599/5031（91.4132%）、分支 1603/1864（85.9979%），对 `agentforge` 全部 44 个 Python 文件启用分支测量，报告与现存文件一致，生产文件修改时间均早于测量；原有 3 行排除未新增。该覆盖率批次自身有 3 个权限跳过；用户后续真实宿主测试单独补充了这三个场景，不代表它们已被纳入覆盖率运行。未覆盖的行和分支仍见机器结果；覆盖率只度量被插桩的测试进程。

135 个冻结路径全部核验，缺失和变化均为 0；模型、运行时关键文件及历史 AF-011 PASS 哈希符合基线。逐项大小和 SHA-256 见 `coverage/TEST_FINAL_INTEGRITY_20261027.json`。本次未启动模型或发送请求。
