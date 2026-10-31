# AF-009E 人工标签审核表

候选数据：`tests/fixtures/retrieval_eval_review_pool_v2.json`  
数据状态：`pending_human_review`  
指定仓库快照：`tests/fixtures/retrieval_eval_repo`，共 8 个文件  
用途：审核完成后，才能将候选复制为正式校准集和独立验证集。本表中的建议均不是人工通过结论。

## 审核规则

每条记录请选择一个结果：

- `接受标签`：查询、相关集合、证据范围和排除理由均正确。
- `修改相关文件集合`：查询有效，但应增加或删除相关文件/符号。
- `证据不足`：当前快照不能支持建议的正例标签。
- `改为无证据查询`：快照中没有足够直接证据。

无证据项还可选择 `改为有证据查询`，并填写路径、行范围和理由。审核人、日期、选择和备注应填入“审核结论”列；在此之前状态保持 `pending_human_review`。

## 相关性边界

1. 查询直接询问“谁调用、谁导入、哪个 handler”时，调用方是主要相关证据；被调用实现只有在回答需要解释目标行为时才加入。
2. 查询询问“如何实现、哪个函数执行转换”时，实际实现方是主要相关证据；仅转发返回值的调用方不自动计入。
3. 查询明确询问“哪里记录、哪个文档说明”时，直接文档可以构成相关证据。查询询问可执行实现时，只有文档提及而没有代码不能算实现证据。
4. 同名符号必须依据限定名称、返回结构和语义区分，不能因名称相同而把所有定义都标为相关。
5. `no_evidence` 只表示上述冻结的 8 文件快照没有足够证据，不表示其他仓库或未来版本无解。

## 有证据候选（20）

| ID | 查询 / 类别 | 建议相关证据与短摘录 | 纳入与排除边界 | 审核问题 | 审核结论 |
|---|---|---|---|---|---|
| candidate-business-continuity | 返回用户如何免重新登录恢复身份？ / 业务概念 | `docs/continuity.md:1-3` “resumes the previously approved principal”；`pkg/session.py:9-11` `return lease.principal` | 纳入政策说明与实现；排除 key rotation/storage | 两个文件都应相关，还是只保留实现？ | 待填写 |
| candidate-cross-file-renewal | 哪个 handler 导入并调用轮换函数？ / 跨文件调用 | `pkg/handlers.py:3-8` `from .session import rotate_session_key`、`return rotate_session_key(key)` | 调用方为主；callee 定义不是所问 handler | 是否只标调用方？ | 待填写 |
| candidate-renewal-implementation | 过期 key 如何替换？ / 概念到实现 | `pkg/session.py:4-6` `return old_key + "-rotated"` | 实现方为主；handler 仅转发 | handler 是否应排除？ | 待填写 |
| candidate-token-lookup | 哪个 lookup 返回 token record？ / 重名消歧 | `pkg/token_cache.py:4-6` `return {"token": key}` | 纳入 token lookup；排除 preference lookup | 限定名称是否充分消歧？ | 待填写 |
| candidate-preference-lookup | 哪个 lookup 返回 display preference？ / 重名消歧 | `pkg/preferences.py:4-6` `return {"preference": name}` | 纳入 preference lookup；排除 token lookup | 限定名称是否充分消歧？ | 待填写 |
| candidate-token-policy-save | 哪里保存 token policy？ / 相似名称 | `pkg/token_cache.py:9-11` `return {"token_policy": choice}` | 排除 save_theme_choice | “保存”是否被此合成返回值充分支持？ | 待填写 |
| candidate-theme-save | 哪里保存 display theme？ / 相似名称 | `pkg/preferences.py:9-11` `return {"theme": choice}` | 排除 save_token_choice | “保存”是否被此合成返回值充分支持？ | 待填写 |
| candidate-storage-separation | 哪里说明 token 与 theme 是不同功能？ / 文档证据 | `docs/storage.md:1-3` “separate synthetic features” | 文档直接回答；两个实现文件仅各说明一侧 | 是否只标文档？ | 待填写 |
| candidate-restore-principal | 哪个函数从 lease 取得 principal？ / 概念到实现 | `pkg/session.py:9-11` `restore_identity_from_lease`、`return lease.principal` | 代码实现为主；continuity 文档不执行 | 是否需要同时标文档？ | 待填写 |
| candidate-coordinator-renew | 哪个类方法续租并保留 principal？ / 嵌套调用 | `pkg/session.py:14-19` `SessionCoordinator.renew` 调用 restore helper | 方法是所问主体；helper 是被调用实现 | 是否将 helper 同列为相关符号？ | 待填写 |
| candidate-continuity-policy | 哪个文档说明 lease 有效时无需新凭证？ / 文档证据 | `docs/continuity.md:1-3` “does not ask for a fresh credential prompt” | 明确问文档；代码未完整陈述政策 | 是否只标文档？ | 待填写 |
| candidate-rotation-output | 哪个实现追加 `-rotated`？ / 实现行为 | `pkg/session.py:4-6` `old_key + "-rotated"` | 实现方直接相关；handler 不执行拼接 | 是否只标 session.py？ | 待填写 |
| candidate-rotation-import | 哪里使用相对导入引入 rotation？ / 跨文件导入 | `pkg/handlers.py:3` `from .session import rotate_session_key` | 只问 import site | 是否只标 handlers.py？ | 待填写 |
| candidate-handler-return | 哪个 boundary handler 返回 rotation 结果？ / 调用方边界 | `pkg/handlers.py:6-8` `handle_expired_session` 返回调用结果 | 明确问 handler，调用方为主 | 是否排除 callee 定义？ | 待填写 |
| candidate-token-return-shape | 哪个 lookup 返回 `token` 字段？ / 重名消歧 | `pkg/token_cache.py:4-6` `{"token": key}` | 返回 schema 排除 preference lookup | 是否存在其他相关文件？ | 待填写 |
| candidate-preference-return-shape | 哪个 lookup 返回 `preference` 字段？ / 重名消歧 | `pkg/preferences.py:4-6` `{"preference": name}` | 返回 schema 排除 token lookup | 是否存在其他相关文件？ | 待填写 |
| candidate-token-policy-return | 哪个 save 返回 `token_policy`？ / 相似名称 | `pkg/token_cache.py:9-11` `{"token_policy": choice}` | 排除 theme save | 返回字段是否足以定义相关性？ | 待填写 |
| candidate-theme-return | 哪个 save 返回 `theme`？ / 相似名称 | `pkg/preferences.py:9-11` `{"theme": choice}` | 排除 token policy save | 返回字段是否足以定义相关性？ | 待填写 |
| candidate-storage-shared-name | 哪个文档解释两个功能都叫 lookup？ / 文档证据 | `docs/storage.md:1-3` “both may use a lookup operation” | 文档提供跨功能解释；代码仅展示实例 | 是否只标文档？ | 待填写 |
| candidate-fixture-nonexecution | 哪里声明合成源码不得作为项目执行？ / 文档证据 | `README.md:1-3` “must never be executed as a project” | 仅 fixture README 陈述该规则 | 此项是否适合作为检索质量正例？ | 待填写 |

## 无证据候选（20）

下表每项均已检查冻结快照的全部 8 个文件；标签含义仅限该快照。

| ID | 查询意图 / 难度 | 排除理由 | 审核问题 | 审核结论 |
|---|---|---|---|---|
| candidate-no-password-reset | 发送密码重置邮件 / 近域 | 无 email、password reset 或 credential recovery 行为 | 是否确实没有足够证据？ | 待填写 |
| candidate-no-account-delete | 永久删除账户 / 近域 | session continuity 不等于 account deletion | 是否存在可被误认为删除的代码？ | 待填写 |
| candidate-no-avatar-upload | 缩放并上传头像 / 域外 | 无 image、resize 或 upload 行为 | 是否保留为域外负例？ | 待填写 |
| candidate-no-invoice-refund | 退款并反向记账 / 域外 | 无 billing、invoice、payment 或 ledger | 是否保留为域外负例？ | 待填写 |
| candidate-no-password-change | 修改已登录用户密码 / 近域 | 无 password mutation 或 credential store | session key rotation 是否会造成标签歧义？ | 待填写 |
| candidate-no-mfa-enroll | 注册 TOTP / 近域 | 无 MFA、TOTP、secret provisioning | 是否为可信近域负例？ | 待填写 |
| candidate-no-oauth-link | 绑定 OAuth identity / 近域 | 无 provider、callback 或 identity linking | lease identity 是否不足以成为相关证据？ | 待填写 |
| candidate-no-logout-all | 退出所有 session / 近域 | 有续租/恢复，无终止所有 session | 正反行为边界是否明确？ | 待填写 |
| candidate-no-invalidate-sessions | 安全事件后使所有 key 失效 / 近域 | 单 key rotation 不等于批量失效 | rotation 是否应明确排除？ | 待填写 |
| candidate-no-revoke-lease | 提前撤销 lease / 近域 | renewal/restoration 不执行 revocation | 撤销与续租是否充分区分？ | 待填写 |
| candidate-no-persist-lease | 将 lease 写入数据库 / 近域 | 只读取参数；无数据库或持久化 | 文档是否暗示持久化？ | 待填写 |
| candidate-no-session-ip-audit | 记录每次续租的 IP / 近域 | 无 IP、audit 或 logging | 是否确实没有审计证据？ | 待填写 |
| candidate-no-rate-limit | 限流过期 session 请求 / 近域 | handler 只有一次调用；无 counter/rate limit | handler 提及是否不足以相关？ | 待填写 |
| candidate-no-delete-token | 按 key 删除 cache record / 近域 | token cache 只有 lookup/save choice | lookup 是否明确不能算 delete？ | 待填写 |
| candidate-no-encrypt-token-cache | 存储前加密 token record / 近域 | 无 crypto、ciphertext 或 encryption | storage 文档是否不足以支持？ | 待填写 |
| candidate-no-token-expiry | 计算 token 到期时间 / 近域 | 无 timestamp、TTL 或 lifetime 字段 | expired session 词汇是否造成歧义？ | 待填写 |
| candidate-no-export-preferences | 导出全部偏好到文件 / 近域 | 无 enumeration、serialization 或 file output | lookup/save 是否明确不足？ | 待填写 |
| candidate-no-theme-contrast | 校验主题颜色对比度 / 近域 | 保存 theme 不执行 color/accessibility validation | 是否为合适的近域困难负例？ | 待填写 |
| candidate-no-locale-preference | 将 locale 应用于翻译 / 近域 | 只有 generic preference/theme；无 locale/i18n | preference lookup 是否明确不足？ | 待填写 |
| candidate-no-compress-cache | 压缩缓存记录 / 近域 | 无 compression、encoding 或 size optimization | storage/cache 词汇是否仍不足以相关？ | 待填写 |

## 调用方与实现方专项审核

请集中比较以下组，避免逐条决定不一致：

- `session_rotation_flow`：`candidate-cross-file-renewal`、`candidate-renewal-implementation`、`candidate-rotation-output`、`candidate-rotation-import`、`candidate-handler-return`。
- 当查询问“handler/import/caller”时，建议只把 `pkg/handlers.py` 作为主要相关文件。
- 当查询问“如何替换/哪个实现追加后缀”时，建议只把 `pkg/session.py` 作为主要相关文件。
- 如果审核人认为回答必须同时看到调用方和实现方，应统一修改这一组的相关集合，不能只修改模型未命中的条目。

## 建议划分的审核

建议划分文件：`tests/fixtures/retrieval_eval_split_plan_v2.json`。

- Calibration：20 条，10 正例、10 无证据；用于审核完成后的阈值选择。
- Validation：20 条，10 正例、10 无证据；在阈值和规则冻结前不得用于选择。
- 同一 `leakage_group` 只出现于一个 split。同义变体、同一函数的多个查询和同一缺失意图不会跨 split。
- 该划分仍为 `proposed_pending_human_review`。若人工修改标签导致类别失衡，应按 group 整体移动，不能拆散组内变体。

审核人最终还需回答：

1. 40 条标签是否逐条接受或已完成修订？
2. 调用方/实现方/文档相关性规则是否统一应用？
3. 20/20 划分是否在审核后的正负标签下仍平衡？
4. 是否确认 Validation 在阈值确定前保持封存？

