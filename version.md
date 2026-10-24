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

2026-10-23：S05 固定 Systran/faster-whisper-small revision `536b0662742c02347bc0e980a01041f333bce120`（MIT），锁定 faster-whisper 1.2.1 / PyAV 18.1.0 / CTranslate2 4.8.3；模型 6 文件共 486,215,847 B，model.bin 官方 LFS/local SHA256 一致。合成中文样本真实离线 worker smoke 返回预期文本（逗号标点不同），receipt 见 `manage/resources/asr-smoke-receipt-20261023.json`。64 MiB/120 秒/解码/busy/cleanup 边界与 reviewer 最终收口待完成，S05/P10 不宣告完整。
2026-10-23：S06 Core 候选迁移闭包增加 target data_root 物化：保留 bundle `media/data_root/...` 副本，同时在 target staging 内生成 reader 所需的 `voice-candidates/{id}.wav`，copy 后复核 SHA256/size，再原子发布。默认 target `Context` 经 `VoiceCandidateService.audio()` 读回定向检查 PASS（1 passed）。官方 Hugging Face transport 仍阻塞，Qwen 权重未就绪、真实双阶段 smoke 未运行；不宣告 runtime readiness。
2026-10-23：S05 有界离线 ASR 独立验收 PASS；最终定向检查 15 passed，复用既有一次合成中文真实 worker smoke，未重跑推理。结果、边界、UI 与用户本机核对步骤已同步至 `manage/reports/S05-ASR-资源与待验收.md`。
2026-10-23：S06 技术 partial 新增隔离 Qwen runtime 配置 `runtimes/qwen-tts/` 与独立锁定环境；Python 3.12、qwen-tts 0.1.1、Transformers 4.57.3、Accelerate 1.12.0、PyTorch/torchaudio 2.8.0+cu128、soundfile 0.13.1 导入，CUDA 12.8、RTX 4060 Laptop GPU 与 BF16 探针通过；没有加载模型。VoiceDesign/Base 分别存在 335,419,103 B 与 469,361,817 B 未校验主权重片段，speech-tokenizer 权重片段为 0 B；可用完整权重为 0 B，双阶段 smoke `blocked_not_run`。
2026-10-23：Qwen runtime 与 worker 允许且仅允许固定 `.cache/huggingface/download/` 中普通 `.metadata`、`.lock`、`.incomplete` sidecar，以及固定 `.cache/huggingface/.gitignore`；真实模型的 allowlist、完整大小/SHA-256/fingerprint 检查保持严格，sidecar 不替代权重。新增窄测试 7 passed；不扫描、移动或删除缓存，不改变 Qwen `needs_resource` 状态。
2026-10-23：仅修正 `manage/host.local.json` 的 `confirmed_design_import_root` 为 `E:/笨蛋小鱼/manage/private/confirmed-design-v4.9`；通过路径存在性检查确认目录，不读取私有内容。其余 host-local 字段未改。

2026-10-23：workflow_review 批准的 S07 technical route 正式接受，canonical acceptance hash `f422062887d2f324f5eab9de7cd39d138f010821e2406d7d7e26d2adc0a3cadf`（report：`H:/AIcode/HexiPlan/runs/cutefish-implementation-20261023-01/reports/acceptance-S07-v1/report.html`；approval：`H:/AIcode/HexiPlan/runs/cutefish-implementation-20261023-01/approvals/0028-step_acceptance.json`）。固定 DeepSeek V4 tokenizer 的本地 `tokenizers==0.23.3` 估算器、typed vision adapter、reserve 后授权/hash/revision 复查与 SQLite 跨实例 CAS；`pytest tests/chat tests/vision -q` 14 passed，重叠 tokenizer 定向子集 5 passed，canonical loader smoke `state=estimated`、`hard_cap=false`、合成中文10 IDs。Rooms 页最终 `npm run build`（vue-tsc + Vite，2,828 modules，7.12s）通过。费用峰值预留保持 ¥2.129920。无付费/live provider API、真实图像推理或浏览器视觉；tokenizer 资源许可未知且 token count 不是供应方硬上限。S07 acceptance 仅覆盖 technical route，overall supplement handoff 的 canonical hash 为 `bf950db95d614f86fff79fc7c2b127ae44f791597e5b7244f353eba9f83a86a2`，结论 `PARTIAL_EXTERNAL_RESOURCE_BLOCKER`（Qwen 权重未完整核验且双阶段 smoke 未运行）。

2026-10-24：S08 本机固定 Qwen 资源与指定短句 VoiceDesign→Base 技术验收正式接受（sequence 189，canonical acceptance hash `e081820a7d9e52273773dd9e5972568a1a351c3c9399e969aaddff965bcada28`）；26 个 allowlisted 文件与4个官方 LFS 权重摘要通过原严格校验器。修复 Windows 子进程非 ASCII 输出被 CP936 编码、服务端按 UTF-8 解码导致的协议失败：worker 通过 `stdout.buffer` 发出显式 UTF-8 JSON；此前定向测试 2 passed、0.06 s，本次收尾未重跑测试、权重 hash、GPU 或模型。attempt02 两阶段服务耗时 48.6646 s / 40.2099 s，worker 耗时 39.6427 s / 40.1604 s，512 tokens、无 OOM retry；两段隔离 WAV 的 SHA-256 及 receipt 见 `../manage/resources/`。人工试听/批准仍 pending；S06 旧 `model_loaded=false` 与 partial 资源观察保留为历史，生产声线和发送均未开启。

2026-10-24：S09 两段 VoiceDesign 试听被用户明确拒绝（“太成熟”），历史保留。S10 两段参考及实验 Mix 的三段 Base `x_vector_only` 试听正式技术验收通过（phasecomplete sequence 241，canonical hash `07d9495d7c7c956b3415aa90655ca906e438eb8e3404549efa16afa9a64c98dc`）；三条同句输出为 24 kHz / 512 tokens / attempt 1 / 无 OOM retry。S10 未登记候选、未批准声线或开启 production，用户主观相似度、混合听感及最终选择仍待反馈；S08 与 S09 历史保留。

2026-10-24：S11 唯一私有批次完成三条分开台词与 instruction 的 Base 语气演绎试听实验，receipt 状态 `complete_private_experimental_expression_listening_required`。A/B/Mix WAV 已按 receipt 字节数与 SHA-256 逐份核对后复制至 `../manage/resources/qwen-voice-expression-s11-{a,b,mix}-20261024.wav`；三段分别 169004/149804/192044 B。独立核对通过的离线试听页为 `E:/tmmpp/三种声线试听.html`（691365 B，SHA-256 `29d949166ac4e91e81f2aec8a4da380f0af1bca15ba6c29bb86eb715a05ec08c`）。用户主观试听仍待完成；不宣称官方保证指令控制，不登记候选、不批准声音、不改 profile、不外发或启用 production；S10/S09 历史保留。
