# CuteFish

CuteFish 是基于 `CuteFish_Codex_Execution_Pack_v4.9` 创建的本机控制面板和助手项目。

## 本机开发

- Python 包：`src/cutefish`
- Python 依赖与锁文件：`pyproject.toml`、`uv.lock`
- 前端：`frontend`（Vue、TypeScript、Vite、Naive UI；npm 锁文件随前端一起管理）
- 项目代理指南与 8 个技能文件已从只读执行包复制到 `.agents/skills` 和 `QUICKSTART_AGENT.md`；源包保持只读。
- 版本和阶段变更：[`version.md`](version.md)
- 用户配置、操作说明与待办集中在工作区的 [`manage`](../manage/README.md) 目录；本机入口见 [快速开始](../manage/快速开始.md)。

S03 bounded offline 范围已独立验收并接受（receipt hash：`a59c864258e8cc77b5876ab6a2266dee09f7d22c70a7aff6af3c6fcecb2d81ae`）。S04 当前内容修订已通过 source quote/content 核对，canonical content hash 为 `bd230c743d85aba798f0dfc5ffaddacf62223aefd85a46a450bce398025c23b5`；最终总验收记录见 [`manage/reports/S04-内容修订与总验收.md`](../manage/reports/S04-内容修订与总验收.md)。本机离线交付材料已收口，完整状态与 Git 交付结果见 [`manage/HANDOFF.md`](../manage/HANDOFF.md)。owner 尚未实际导入、试聊或批准；P03 token 容量与 P07 视觉 adapter 仍为 partial，目标机和 production 仍未验证。已填私稿保存在 Git 仓库外私有目录，源码不含私稿正文。

本项目当前仅支持手动本机试用；不配置开机启动，不自动发送真实消息，也不自动调用付费服务。执行入口与用户设置见 `manage`。
