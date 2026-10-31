# AF-009E AI 证据审查

日期：2026-10-26  
方法：`ai_evidence_review`  
人工审核：`false`

## 结论与边界

本报告逐行阅读冻结的 8 文件合成仓库，对 40 条候选进行静态证据审查。审查不执行合成代码，不参考检索排名或指标来改变标签，也不改变原候选池和 split plan。

裁定统计：

| 裁定 | 数量 |
|---|---:|
| `accept_current` | 17 |
| `modify_relevant_set` | 0 |
| `insufficient_evidence` | 0 |
| `no_evidence` | 20 |
| `rewrite_query` | 3 |

38 条为 `high` 置信度，2 条为 `medium`。置信度是证据审查把握程度，不是统计校准概率。所有结果都是 AI 建议，仍然不是人工通过的 ground truth；不得用本文件启动正式阈值校准。

冻结检查范围：`README.md`、`docs/continuity.md`、`docs/storage.md`、`pkg/__init__.py`、`pkg/handlers.py`、`pkg/preferences.py`、`pkg/session.py`、`pkg/token_cache.py`。

## 有证据候选（20）

| ID | 裁定 / 置信度 | 建议证据 | 排除与理由 | 不确定性 |
|---|---|---|---|---|
| candidate-business-continuity | `accept_current` / medium | `docs/continuity.md:1-3` 明述恢复 principal 且无需新凭证；`pkg/session.py:9-11` 返回 `lease.principal` | 排除 rotation、handler 和 storage；它们不回答身份恢复 | “Where does” 可被窄解为只问实现，但 no-prompt 条件只在文档中直接出现 |
| candidate-continuity-policy | `accept_current` / high | `docs/continuity.md:1-3`：“does not ask for a fresh credential prompt” | `pkg/session.py` 未完整陈述政策 | 无实质不确定性 |
| candidate-coordinator-renew | `rewrite_query` / high | `pkg/session.py:14-19`：`renew` 委托 `restore_identity_from_lease` | helper 不是所问类方法；函数体没有 lease 延期操作 | 建议改为“哪个类方法委托从已批准 lease 恢复 principal？”；原查询的“extends”仅由名称/docstring 支持 |
| candidate-cross-file-renewal | `accept_current` / high | `pkg/handlers.py:3-8`：相对导入并在 handler 中调用 | `pkg/session.py` 是 callee 定义，不是所问 caller | 无实质不确定性 |
| candidate-fixture-nonexecution | `accept_current` / high | `README.md:1-3`：“must never be executed as a project” | 功能代码和功能文档未陈述夹具执行规则 | 是否具有生产代表性是评测设计问题，不是证据错误 |
| candidate-handler-return | `accept_current` / high | `pkg/handlers.py:6-8`：handler 返回 rotation 调用结果 | `pkg/session.py` 实现 rotation，但不是 boundary handler | 无实质不确定性 |
| candidate-preference-lookup | `accept_current` / high | `pkg/preferences.py:4-6`：返回 `{"preference": name}` | `pkg/token_cache.py::lookup` 返回 token | 无实质不确定性 |
| candidate-preference-return-shape | `accept_current` / high | `pkg/preferences.py:4-6`：字面返回键 `preference` | token lookup 返回键 `token` | 无实质不确定性 |
| candidate-renewal-implementation | `accept_current` / high | `pkg/session.py:4-6`：`old_key + "-rotated"` | handler 只转发，不实现转换 | 仅证明合成字符串转换，不构成安全性声明 |
| candidate-restore-principal | `accept_current` / high | `pkg/session.py:9-11`：`return lease.principal` | continuity 文档不是实现 | “authorized” 来自夹具语义，代码没有独立授权检查 |
| candidate-rotation-import | `accept_current` / high | `pkg/handlers.py:3`：`from .session import rotate_session_key` | 定义文件不导入自身 | 无实质不确定性 |
| candidate-rotation-output | `accept_current` / high | `pkg/session.py:4-6`：追加 `-rotated` | handler 未执行追加 | 无实质不确定性 |
| candidate-storage-separation | `accept_current` / high | `docs/storage.md:1-3`：明确称两者是 separate features | 两个实现模块各自只说明一侧 | 无实质不确定性 |
| candidate-storage-shared-name | `accept_current` / medium | `docs/storage.md:1-3`：separate features 可同时使用 lookup | 两个实现模块只展示实例，不作跨功能解释 | 文档给出的是简短陈述，而不是深入解释“为什么” |
| candidate-theme-return | `accept_current` / high | `pkg/preferences.py:9-11`：返回 `{"theme": choice}` | token save 返回 `token_policy` | 查询只问返回 mapping，未声称真正持久化 |
| candidate-theme-save | `rewrite_query` / high | `pkg/preferences.py:9-11`：构造并返回 theme mapping | 无代码持久化 theme；token save 属于另一语义 | 建议改为“哪个函数返回所选 display theme 的 mapping？”；docstring 的 Save 不能替代存储实现 |
| candidate-token-lookup | `accept_current` / high | `pkg/token_cache.py:4-6`：返回 `{"token": key}` | preference lookup 返回 display preference | 无实质不确定性 |
| candidate-token-policy-return | `accept_current` / high | `pkg/token_cache.py:9-11`：返回 `{"token_policy": choice}` | theme save 返回 `theme` | 查询只问返回 mapping，未声称真正持久化 |
| candidate-token-policy-save | `rewrite_query` / high | `pkg/token_cache.py:9-11`：构造并返回 token-policy mapping | 无代码持久化 policy；theme save 属于另一语义 | 建议改为“哪个函数返回所选 token policy 的 mapping？”；docstring 的 Save 不能替代存储实现 |
| candidate-token-return-shape | `accept_current` / high | `pkg/token_cache.py:4-6`：字面返回键 `token` | preference lookup 返回键 `preference` | 无实质不确定性 |

## 无证据候选（20）

以下每条均检查了完整冻结范围，而不是依据词法检索未命中作结论。`no_evidence` 仅表示该 8 文件快照没有满足查询意图的证据。

| ID | 裁定 / 置信度 | 检查与主要排除理由 |
|---|---|---|
| candidate-no-account-delete | `no_evidence` / high | continuity、principal 恢复和 key rotation 均不是永久账户删除；无账户删除实现或文档 |
| candidate-no-avatar-upload | `no_evidence` / high | 无 image、avatar、resize、media 或 upload 行为 |
| candidate-no-compress-cache | `no_evidence` / high | token cache 返回 mappings；storage 文档只区分功能；无 compression/encoding/compaction |
| candidate-no-delete-token | `no_evidence` / high | `pkg/token_cache.py:4-11` 只有 lookup 和返回 token-policy mapping，无删除 |
| candidate-no-encrypt-token-cache | `no_evidence` / high | token cache 返回明文 dictionary；无 encryption、ciphertext、key management 或持久化 |
| candidate-no-export-preferences | `no_evidence` / high | preference 模块只有单项 lookup/theme mapping；无枚举、序列化或文件输出 |
| candidate-no-invalidate-sessions | `no_evidence` / high | 单个 key 字符串转换不等于全量失效；无 registry、批处理或 incident response |
| candidate-no-invoice-refund | `no_evidence` / high | 无 billing、invoice、payment、refund 或 ledger 内容 |
| candidate-no-locale-preference | `no_evidence` / high | generic preference/theme 不包含 locale、translation 或 i18n |
| candidate-no-logout-all | `no_evidence` / high | restore/rotate 不终止 session；无 session enumeration 或 logout |
| candidate-no-mfa-enroll | `no_evidence` / high | 无 MFA、TOTP、secret provisioning 或 enrollment |
| candidate-no-oauth-link | `no_evidence` / high | lease restoration 不包含 provider、callback、authorization code 或 linking |
| candidate-no-password-change | `no_evidence` / high | no-prompt 政策不等于 credential mutation；无 password/hash/account settings |
| candidate-no-password-reset | `no_evidence` / high | 无 email、password reset、recovery token 或通知逻辑 |
| candidate-no-persist-lease | `no_evidence` / high | session 代码只读取传入 lease；无 database/schema/write operation |
| candidate-no-rate-limit | `no_evidence` / high | `pkg/handlers.py:6-8` 直接调用一次；无 counter、clock、quota 或拒绝分支 |
| candidate-no-revoke-lease | `no_evidence` / high | restoration/renew delegation 不执行 revoke、blacklist、delete 或 expiration mutation |
| candidate-no-session-ip-audit | `no_evidence` / high | `renew` 只接收 lease；无 IP、request metadata、audit event 或 logging |
| candidate-no-theme-contrast | `no_evidence` / high | theme mapping 不解析颜色或验证 accessibility/contrast |
| candidate-no-token-expiry | `no_evidence` / high | token mappings 不含 timestamp、TTL、clock 或 expiry calculation |

## 高风险歧义与后续人工关注

必须优先人工复核的 3 条 `rewrite_query`：

1. `candidate-coordinator-renew`：代码没有实际延期 lease。
2. `candidate-theme-save`：函数返回 mapping，但没有持久化 theme。
3. `candidate-token-policy-save`：函数返回 mapping，但没有持久化 token policy。

中等置信度但暂接受当前集合的 2 条：

1. `candidate-business-continuity`：业务问题是否应同时标注政策文档与实现文件。
2. `candidate-storage-shared-name`：文档对“为什么”的解释很简短。

这些条目没有被强行标记为通过。正式人工审核仍需决定是否接受改写、修改相关集合或将证据视为不足。

## 数据隔离声明

- 原始 v1、AF-009D 候选、AF-009E 候选池和 split plan 均保持原样。
- AI 结果单独保存在 `tests/fixtures/retrieval_eval_review_pool_v2_ai_adjudicated.json`。
- 所有记录明确为 `review_method: ai_evidence_review`、`human_reviewed: false`。
- 未重新分组；Calibration/Validation 分配和 `leakage_group` 原样复制。
- 本产物不得作为正式人工标签，不得据此计算或发布正式评测指标。
