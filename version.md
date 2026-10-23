0.1.0.dev1

2026-10-23：建立新项目版本基线；尚未发布。
2026-10-23：S01-P00/P01/P02 建立 `src/cutefish` Python 包、uv 依赖锁、host path 与手动 loopback 底座；S01 独立验收 PASS 并按 receipt 接受。
2026-10-23：增加 Vue/TypeScript/Vite/Naive UI 前端锁文件；typecheck/build、静态首页与核心隔离 ASGI 检查通过，视觉检查 NOT_RUN。
2026-10-23：从只读 v4.9 执行包复制 8 个 `.agents/skills` 文件及 `QUICKSTART_AGENT.md`，并逐项校验 SHA256；源包未修改。

2026-10-23：S02（P03–P06）开始；Astra 完成初期框架设计，Luna 按方案实施，保留已验收 S01 底座。
2026-10-23：为 DeepSeek OpenAI 兼容异步客户端加入 `openai>=1.55,<2.0`，锁定 `1.109.1`；只验证本地 AsyncOpenAI/base_url/max_retries 签名，未读取密钥或调用 API。


2026-10-23：S02-v2 补充范围获批；S02 同步准备媒体 manifest/排除规则、只读 TTS 能力探测与 SQLite Online Backup 隔离钩子，未访问真实外部目标。计划 hash：605d0633b9ec3e5d39f645f5d59c3a7c571464d8d06024826a40d6fcf6890a8a。



2026-10-23：S02 core 修复 PersonaVersion 索引重名；Context 接入 Chat/Memory/QQ 服务并显式挂载 chat/persona/memory/qq 路由；为既有 S02 persona_approval 增加三项 nullable 证据列兼容；extensions 持久模型加入显式注册。临时 SQLite 验证通过，未调用真实 DeepSeek API 或 QQ 发送器。

2026-10-23：按 workflow_review 批准的 S02-r3 继续实施（计划 hash：5528031d50c18cef9af21d6c0bdcb75db08f0e24554c70ace5049d8c1b3a0fe4）；补入人格版本/访谈流程、持久任务候选与房间窄操作，以及 P13 离线文档、manifest/schema/template 和薄 CLI 准备。未授权真实迁机或正式启用。
2026-10-23：新增 P13 迁移说明与 Manifest v1 schema/template。导出约束为隔离 Online Backup 副本清理敏感表、干净重建并检查通过后生成包；实际 pipeline/CLI 尚未完成，本轮没有读取或导出真实业务数据库。
2026-10-23：S02源码盘点确认 QQ账号/身份与 Outbox 草稿/预检已实现；真实发送器和 Outbox 派发状态机仍在实施整合中，当前不对外发送。Astra 已补齐实施合同，待代码稳定后再更新阶段报告。
2026-10-23：补齐 QQ reverse HTTP 入站鉴权、原文幂等持久化、owner-only local_trial 与 1 秒静默窗/5 秒硬上限合并；事件经 ChatService 转入 Outbox 草稿链路。添加仅显式装配的 NapCat OneBot 私聊 sender，默认 Outbox sender 仍为 None；ChatService 增加忙碌会话有限等待。fake-only SQLite/HTTP transport 定向检查通过，未调用真实 DeepSeek 或 QQ 网络。
2026-10-23：修复 SQLite 立即外键检查下人格草案与记忆纠正的写入顺序；补充有限定向 fake-provider 检查，确认反馈跨 session 持久、记忆人物/范围隔离与UTC日期序列化、同会话并发CAS只派发一次、纠正期间in-flight回复和旧草稿转stale、旧提示指纹在预算预留前拒绝。未调用真实模型。
2026-10-23：整理 S03 集成接口合同草案，覆盖 B站/Hexi 来源证据与受限能力、动作预览 hash/owner 授权/统一 Outbox、官方 Codex 事件与指定房间只读适配。当前仅为规划，等待 S02 通过及 S03 放行后实施；真实站点 NOT_RUN。
2026-10-23：在 `manage/prompts/` 建立单任务提示模板并从管理 README 链接；具体真人格、QQ、语音、站点和目标机步骤待相应功能实际完成后再按能力拆分。
2026-10-23：S02 核心实现定向证据：Outbox dispatcher 双并发 CAS 仅一次派发、quota/auth 一次性检查；fake sender exception/cancel 转 `unknown` 不重放，启动时残留 `sending` 转 `unknown`；Context 接入动态 QQ identity/takeover；DeepSeek 凭据采用 typed write-only GET/PUT；v1→v2 migration probe PASS。sender 仍为 `None`，未真实调用 QQ/DeepSeek，最终阶段验收待 reviewer 收尾。
2026-10-23：修正候选本地预览使用 GET 路由；房间来源引用按结构化 SourceReference DTO 提交和显示；设置页加入 DeepSeek 凭据状态/写入表单，成功后清空页面内存中的 key，不回显、不测试连通性。前端 `npm run build`（vue-tsc + Vite）通过。

2026-10-23：S02（P03–P06）独立验收 PASS（hash：`199e13d6e684e7f5cb3f5b4201cbc154ca1a07e45de9d38de62a119fba099e22`）；workflow_review 放行 S03（计划 hash：`f44910c57965988a33ec7f4d55bc044fdd9081ca040bf510d9831ec950b62305`）。S03 当前实施中，独立验收未完成。
2026-10-23：新增 integrations 能力/读取/动作预览路由，来源引用由服务端 EvidenceStore 解析并绑定预览；固定注册进 Context allowlist，保持 disabled/NOT_CONNECTED，production read/action registries 为空。此前定向 mock/unit 检查 16 passed、compileall 通过；未访问真实站点或外发。
2026-10-23：加入 P00–P14 管理状态源 `manage/feature-status.json` 并将 S03 当前状态、限制和证据同步到管理文档。P13 薄 CLI 只暴露 `validate` 与 `import-staging`；`.venv` 下 CLI `--help` smoke exit 0。没有读取源数据库、inspect/export、快照、迁移导入或目标机验证。
2026-10-23：单独调用 integrations registrar 的无副作用 smoke 通过：静态登记 14 项能力，`enabled=false`、`NOT_CONNECTED`、`action_handler=None`；未访问网络或数据库。
2026-10-23：为 integrations capabilities DTO 加入独立 `read_registered`，按已装配 read registry 派生；动作仍看 handler registry，目标配置仍保持真实 alias 状态。新增定向回归 1 passed；UI 需同时检查读 connector 与目标配置后才允许读取。
2026-10-23：收紧四阶段 Feature DTO（本机/目标失败态及旧目标标签映射），允许 schema 1/2/3 仅经已知加法升级到4并保留 schema2 `chat_request.source_revision`；迁移只对白名单 `chat_message.token_usage_json` 放行并校验用量/元数据、递归拒绝敏感键。修正 Windows manifest fsync 使用只读句柄失败。隔离合成库迁移导出、校验、staging 导入与旧 schema 探针 PASS；源库 hash 不变，目标 staging 门禁拒绝外发/付费/批次/主动操作。未操作真实业务库或本机 room root。
2026-10-23：S04 接入可选 `confirmed_design_import_root` host setting，缺省关闭且仅限 `manage/private` 子路径；Context 将其注入 PersonaService。加载设置时不探测私有目录，导入仍须显式 owner 操作。
2026-10-23：独立 review 后将 P07/P10/P11 状态收紧为 partial，明确视觉能力/成本、SAPI 表达限制、ASR 模型和 NapCat codec 尚未证实；未把代码存在当作端到端验收。
2026-10-23：更新管理文档，说明 DeepSeek 本机 Key 表单只保存凭据、不测试连通性；新增分步 TXT 提示并为试聊、QQ、语音、站点和未来迁移/启用设置验收门槛。无真实外部操作。

2026-10-23：进入 workflow_review 批准的 S04（计划 hash：8522a37a703c24d176db6551a17299863310fcdbc7c72f3e8f36978d5cdd984f）；私有设定仅提供 owner 显式导入为未批准草案。窄样本、配置接线和受影响 UI build 通过，reviewer receipt 收口中。真实 bundle 尚未导入；P03 token 容量仍为 partial，P07 仍需视觉 image request/response adapter。未执行真实 API、QQ、迁移或 Git 提交/推送。
2026-10-23：S04 私有 bundle 内容收敛：当前有效 bundle hash `7dd629351c07e8aa89bb4d3b50ebd223f446ba41759a0758c6b95c86a0aeed52` 取代初版 `519022547ea93fca2b30c815fdfcdb93ebf0dfe63f9314d0825341a00c24eef9`；源文件 SHA-256 不变，Persona 内容限定为 ROLE_DRAFT 全文与 CONFIRMED_DESIGN 的角色相关选段。仅待一次 loader quote/content 核对和 reviewer note；未做 UI/API 重测或 owner 导入。
2026-10-23：S04 当前 bundle (`7dd629351c07e8aa89bb4d3b50ebd223f446ba41759a0758c6b95c86a0aeed52`) 的 source quote/content 核对 PASS，canonical content hash `bd230c743d85aba798f0dfc5ffaddacf62223aefd85a46a450bce398025c23b5`（1,411 字符）；最终 workflow reviewer 总验收报告已完成。本机离线交付材料已收口，Git 交付结果见工作区外部管理目录 `manage/HANDOFF.md`。owner 未实际导入/试聊/批准，目标机和 production 仍未验证；P03 token 容量与 P07 视觉 adapter 保持 partial。