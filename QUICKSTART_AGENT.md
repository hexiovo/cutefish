# QUICKSTART_AGENT / CuteFish Codex 4.9

> 先读本文件与 AGENTS.md；S10 三段 Qwen 参考试听技术验收已接受，用户主观选择仍待反馈。项目实施、本机核验、目标核验和正式启用分开记录；源执行包只读。

当前状态：S10 已 ACCEPTED / phasecomplete（sequence 241），canonical acceptance hash `07d9495d7c7c956b3415aa90655ca906e438eb8e3404549efa16afa9a64c98dc`；S10 两段参考与实验 Mix 的三条 Base `x_vector_only` 试听已完成技术验收。S09 两段试听被用户以“太成熟”拒绝并保留。S10 三段的主观相似度、Mix 听感和最终选择仍待反馈，未登记候选、未批准声音或启用 production；S08 acceptance 与 supplement-03 handoff-v2 保留为历史。详见 `../manage/reports/acceptance-S10-v1/report.html` 和 receipt `../manage/reports/acceptance-S10-v1/0041-step_acceptance.json`。
唯一交付状态源：`../manage/feature-status.json`，按 P00–P14 分别记录实现、本机核验、目标核验和正式启用证据。
当前 Git 提交/推送结果记录在工作区外部管理目录 `../manage/HANDOFF.md`。不扩大审查、不重跑旧验证。owner 尚未导入 bundle 或启动 calibration 试聊；当前仅 local_trial 手动 loopback，不访问真实站点、不调用付费 API、不外发、不执行迁移。

## 阶段与证据
| 阶段 | 状态 | 简述 | 报告 |
|---|---|---|---|
| P00 | accepted | 新工程、用户约束和 local_trial 边界已接手；目标机未知 | `../manage/reports\P00-接手与设计导入.md` |
| P01 | accepted | 8 个项目 skills 哈希一致；Python/uv 与前端依赖就绪；typecheck/build 通过 | `../manage/reports\P01-项目Skills与最小依赖.md` |
| P02 | accepted | 本机模块/API/host path 底座及静态首页接口通过；browser visual NOT_RUN | `../manage/reports\P02-模块底座与本地UI.md` |
| P03 | partial | 聊天与预算账本离线路径通过；当前上下文只有 UTF-8 byte 上限，token 容量目标 partial；未调用真实 API | `../manage/feature-status.json` |
| P04 | accepted | 人格版本/访谈和隔离记忆离线验收；S04 为 ROLE_DRAFT 全文加 CONFIRMED_DESIGN 角色相关选段的显式幂等未批准导入 | 同上；当前内容核对已通过 |
| P05 | accepted | QQ 身份和 Outbox 离线链路验收；无真实配对或外发 | 同上；状态见 feature-status.json |
| P06 | accepted | 管理面板和 API 接线验收；浏览器视觉 NOT_RUN | 同上；状态见 feature-status.json |
| P07 | partial | 仅有本地图片解码；仍需图像请求/响应 adapter 代码，视觉能力与计价未证实 | feature-status.json |
| P08 | partial | S03 受限集成基础与 mock 测试；站点目标/真实 handler 未配置 | `../manage/reports\S03-集成接口合同.md` |
| P09 | partial | 持久候选/本地控制已有；主动调度和真实互动未启用 | feature-status.json |
| P10 | partial | S05 ASR 与 S08 技术范围已验收；S09 试听被拒，S10 三段参考试听技术验收通过，等待用户主观选择 | `../manage/feature-status.json` 与 `../manage/reports/acceptance-S10-v1/report.html` |
| P11 | partial | QQ语音发送结构有部分实现；NapCat codec 未实证，默认仍文字 | feature-status.json |
| P12 | partial | S03 证据绑定动作预览；生产 handler 为空、未真实派发 | `../manage/reports\S03-集成接口合同.md` |
| P13 | partial | 迁移说明/schema/template 和薄 CLI；只验证帮助输出，未运行迁移 | `../manage/迁移说明.md` |
| P14 | local complete | 本机交接材料与最终验收副本已收口；目标机未知 | 目标机交接与 production 启用待后续独立完成 |
## 人工交接要点
### active_context
已接受的各阶段范围与边界见 `../manage/feature-status.json` 及 `../manage/HANDOFF.md`。S10 三段参考试听技术范围已 ACCEPTED / phasecomplete（sequence 241），canonical hash `07d9495d7c7c956b3415aa90655ca906e438eb8e3404549efa16afa9a64c98dc`；S09 “太成熟”的主观拒绝保留，S10 主观选择仍待用户反馈。S06 partial/blocked、S08 acceptance 和 supplement-03 handoff-v2 保留为历史。当前没有待办的HexiPlan审批或模型验证；保留 local_trial 与所有外部/production边界。
### decisions
```json
[
  "Codex开发、真实DS说话；统一亲情妹妹感；owner唯一工具权、按人记忆",
  "每日总预算8元、后台封顶6元；owner 131072 tokens/contact 32768 tokens 是尚未达成的输入容量目标，当前仅为相同数值的 UTF-8 byte 上限",
  "默认local_trial：手动、owner样本、有限任务，不自启/自动批量浏览发布",
  "小红书移除；历史源表不是当前授权，hexi与B站继续开发",
  "本机 room_root 由 host.local.json 指定；声音来源都可以；S10 已使用用户提供的两段参考完成三段技术试听，S09“太成熟”的拒绝保留，S10 主观选择仍待反馈；不付费，默认文字除非要求",
  "hexi用户选env.txt本机输入，Q15.b业务类别都可以；保留受限账号和私密素材排除，当前预览优先",
  "当前路径只作host-local映射，目标OS/硬件/路径未知；不重填已明确事项",
  "迁移保留人格记忆纠正/删除/作品/声线/费用，目标凭据和依赖重新核验；默认禁止外发调度",
  "P05仅本机小批量QQ试用，P14仅本机交付；未来A25迁机/A26正式启用",
  "已填私稿原字节归档在仓库外 manage/private/confirmed-design-v4.9；Persona 草案只由 ROLE_DRAFT 全文及 CONFIRMED_DESIGN 的角色相关选段组成，owner需登录 /persona 手动导入，试聊仍需另行明确启动",
  "阶段完成后更新交接并按已批准计划继续；仅暂停确需用户输入或专项授权的事项，不阻断其他离线工作"
]
```
### environment
```json
{
  "current_host": "已核验 Python 3.12.0、uv、Node.js 22.20.0/npm 10.9.3；目标系统信息未知",
  "target_host": "not_provided",
  "profile": "local_trial"
}
```
### running_processes
```json
[]
```
### uncommitted_changes
```json
[]
```
### blockers
```json
[
  "owner 尚未在 Persona 页面导入 bundle 或进行 DeepSeek calibration 试聊；真人反馈与人格批准未完成",
  "S10 三段隔离 WAV 等待用户主观相似度、Mix 听感和最终选择反馈；当前没有获批声音",
  "browser visual 为 NOT_RUN；没有真实浏览器验收",
  "P03 token 容量目标未达成；P07 adapter 已 code-ready，但真实 provider 能力、价格与图片推理未验证",
  "目标机未知不阻塞当前本机实施"
]
```
### next_exact_action
如 owner 愿意，按 `../manage/prompts/语音试听.txt` 收听 `../manage/resources/qwen-voice-reference-s10-a-20261024.wav`、`qwen-voice-reference-s10-b-20261024.wav` 与 `qwen-voice-reference-s10-mix-20261024.wav`，反馈相似度、Mix 听感和最终选择；不重跑模型或资源校验。S09“太成熟”的拒绝与旧历史保留。之后仅按用户新指示推进仍待真人输入或专项验证的事项。
### accepted_not_implemented
```json
[
  "N01",
  "N02",
  "N03",
  "N04",
  "N05",
  "N06",
  "CX01",
  "CX02",
  "CX03",
  "CX04",
  "CX05",
  "CX06",
  "CX07",
  "CX08",
  "BH01",
  "BH02",
  "BH03",
  "BH04",
  "EM01",
  "EM02",
  "EM03",
  "EM04",
  "EM05",
  "C01",
  "C02",
  "C03",
  "C04",
  "C05",
  "C06",
  "C07",
  "SITE02",
  "SITE03",
  "SITE04",
  "DEP01",
  "DEP02",
  "DEP03",
  "DEP04",
  "DEP05",
  "DEP06",
  "DEP07"
]
```
### migration
```json
{
  "source_host_id": null,
  "target_host_id": null,
  "source_sender_stopped": null,
  "snapshot_created": false,
  "target_verified": false,
  "production_enabled": false
}
```

## 必守设计
一个亲情妹妹感角色，不是情侣；owner固定最高亲近且唯一命令权；普通人只聊天、记忆独立。
Codex开发，DeepSeek实际说话；UI与自然度优先。后台插件优先，不抢鼠标；按需释放重型资源。
原文逐事件落盘，不循环摘要；已填总8元/后台6元封顶，最少相关检查。
按需读取STAGE_ROADMAP.md、FEATURE_MATRIX.md和当前stages任务书；不要重读全部来源。
尚需确认事项以当前待办清单为准；已填私稿正文保存在仓库外的私有归档，不能复制进源码或公开报告。用户尚未执行 Persona 导入或 calibration 试聊。
4.9取消小红书；本机local_trial小批量，目标以后配置。段落/重点语音见specs19—21；hexi见specs23；迁移见specs24。
C07语气停顿重音是P10/P11核心；不付费语音，默认文字除非要求；hexi/B站小量读取，公开操作先预览。
可难过、生气、有脾气；非恒定积极、无情绪分值，真实DS校准，权限/停止和数据不受情绪影响。
本机 room_root 由 host.local.json 指定，路径不能沿用到未知目标机；不扫描其他盘，已填不重问。
owner最高128K/普通最低32K，后台任务旧逻辑；作息软偏好，明确DND/预算/权限硬边界；未运行不声称启用。




