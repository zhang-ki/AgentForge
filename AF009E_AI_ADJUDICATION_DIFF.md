# AF-009E AI 裁定差异清单

本清单比较原始 `retrieval_eval_review_pool_v2.json` 与独立 AI 建议文件。原文件未被修改；“无变化”也只表示 AI 建议保留原候选标签，不代表人工通过。

| ID | 原标签 | AI 建议 | 差异与依据 |
|---|---|---|---|
| candidate-business-continuity | evidence: `docs/continuity.md`, `pkg/session.py` | `accept_current`，同集合 | 无集合变化；文档支持 no-prompt，代码返回 principal；业务问题可能需要两类证据 |
| candidate-continuity-policy | evidence: `docs/continuity.md` | `accept_current`，同集合 | 无变化；文档直接陈述规则 |
| candidate-coordinator-renew | evidence: `pkg/session.py` / `SessionCoordinator.renew` | `rewrite_query` | 原问题声称实际延期 lease，但函数体仅委托 principal 恢复；建议改问“哪个类方法委托恢复 principal” |
| candidate-cross-file-renewal | evidence: `pkg/handlers.py` / handler | `accept_current`，同集合 | 无变化；caller 包含 import 和 call |
| candidate-fixture-nonexecution | evidence: `README.md` | `accept_current`，同集合 | 无变化；README 直接陈述不得执行 |
| candidate-handler-return | evidence: `pkg/handlers.py` / handler | `accept_current`，同集合 | 无变化；查询明确问 boundary caller |
| candidate-no-account-delete | no evidence | `no_evidence` | 无标签变化；完整快照无账户删除 |
| candidate-no-avatar-upload | no evidence | `no_evidence` | 无标签变化；完整快照无图像或上传 |
| candidate-no-compress-cache | no evidence | `no_evidence` | 无标签变化；无压缩或存储优化 |
| candidate-no-delete-token | no evidence | `no_evidence` | 无标签变化；token cache 只有 lookup 和 mapping 返回 |
| candidate-no-encrypt-token-cache | no evidence | `no_evidence` | 无标签变化；无加密或持久化 |
| candidate-no-export-preferences | no evidence | `no_evidence` | 无标签变化；无枚举、序列化或文件导出 |
| candidate-no-invalidate-sessions | no evidence | `no_evidence` | 无标签变化；单 key 转换不是全量失效 |
| candidate-no-invoice-refund | no evidence | `no_evidence` | 无标签变化；无账单、退款或 ledger |
| candidate-no-locale-preference | no evidence | `no_evidence` | 无标签变化；无 locale 或 translation |
| candidate-no-logout-all | no evidence | `no_evidence` | 无标签变化；无 session 终止或枚举 |
| candidate-no-mfa-enroll | no evidence | `no_evidence` | 无标签变化；无 MFA/TOTP/enrollment |
| candidate-no-oauth-link | no evidence | `no_evidence` | 无标签变化；无 OAuth/linking |
| candidate-no-password-change | no evidence | `no_evidence` | 无标签变化；无 credential mutation |
| candidate-no-password-reset | no evidence | `no_evidence` | 无标签变化；无 email/password reset |
| candidate-no-persist-lease | no evidence | `no_evidence` | 无标签变化；无数据库或 lease 写入 |
| candidate-no-rate-limit | no evidence | `no_evidence` | 无标签变化；handler 无计数、时钟或拒绝分支 |
| candidate-no-revoke-lease | no evidence | `no_evidence` | 无标签变化；无 revoke/blacklist/delete |
| candidate-no-session-ip-audit | no evidence | `no_evidence` | 无标签变化；无 IP 或 audit logging |
| candidate-no-theme-contrast | no evidence | `no_evidence` | 无标签变化；无颜色或可访问性验证 |
| candidate-no-token-expiry | no evidence | `no_evidence` | 无标签变化；无 timestamp/TTL/expiry |
| candidate-preference-lookup | evidence: `pkg/preferences.py` / lookup | `accept_current`，同集合 | 无变化；return key 为 `preference` |
| candidate-preference-return-shape | evidence: `pkg/preferences.py` / lookup | `accept_current`，同集合 | 无变化；字面返回 schema 唯一消歧 |
| candidate-renewal-implementation | evidence: `pkg/session.py` / rotate | `accept_current`，同集合 | 无变化；函数体追加 `-rotated` |
| candidate-restore-principal | evidence: `pkg/session.py` / restore helper | `accept_current`，同集合 | 无变化；函数直接返回 `lease.principal` |
| candidate-rotation-import | evidence: `pkg/handlers.py` | `accept_current`，同集合 | 无变化；第 3 行是相对导入 |
| candidate-rotation-output | evidence: `pkg/session.py` / rotate | `accept_current`，同集合 | 无变化；实现体执行后缀追加 |
| candidate-storage-separation | evidence: `docs/storage.md` | `accept_current`，同集合 | 无变化；文档直接称 separate features |
| candidate-storage-shared-name | evidence: `docs/storage.md` | `accept_current`，同集合 | 无集合变化；证据简短，因此置信度 medium |
| candidate-theme-return | evidence: `pkg/preferences.py` / save theme | `accept_current`，同集合 | 无变化；查询只问返回 theme mapping |
| candidate-theme-save | evidence: `pkg/preferences.py` / save theme | `rewrite_query` | 函数只返回 mapping，没有存储；建议改问“哪个函数返回 theme mapping” |
| candidate-token-lookup | evidence: `pkg/token_cache.py` / lookup | `accept_current`，同集合 | 无变化；return key 为 `token` |
| candidate-token-policy-return | evidence: `pkg/token_cache.py` / save token | `accept_current`，同集合 | 无变化；查询只问返回 token_policy mapping |
| candidate-token-policy-save | evidence: `pkg/token_cache.py` / save token | `rewrite_query` | 函数只返回 mapping，没有存储；建议改问“哪个函数返回 token-policy mapping” |
| candidate-token-return-shape | evidence: `pkg/token_cache.py` / lookup | `accept_current`，同集合 | 无变化；字面返回 schema 唯一消歧 |

## 实质变更建议

只有以下 3 条建议改变查询文本；原候选未被覆盖：

| ID | 原查询问题 | 建议查询 |
|---|---|---|
| candidate-coordinator-renew | 声称类方法实际 extends lease | Which class method delegates recovery of the principal from an approved lease? |
| candidate-theme-save | 声称 theme 被 stored | Which function returns a mapping for the chosen display theme? |
| candidate-token-policy-save | 声称 token policy 被 stored | Which function returns a mapping for the selected token policy? |

## 仍需人工复核

- 上述 3 条 `rewrite_query`：决定接受改写、改为证据不足，或补充合成代码证据。
- `candidate-business-continuity`：决定业务概念查询是否同时保留政策文档和实现文件。
- `candidate-storage-shared-name`：决定当前一句文档是否足以满足“explains why”。

在人工决定前，这 5 条不能视为正式通过；其余 AI 裁定同样不等于人工审核。
