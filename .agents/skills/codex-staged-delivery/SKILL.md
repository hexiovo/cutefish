---
name: codex-staged-delivery
description: 逐阶段交付、收尾、恢复和子代理协作时使用。
---

# codex-staged-delivery

读取AGENTS、QUICKSTART、当前P阶段；仅做用户指定阶段。允许1—2真实子代理，分文件边界。少量相关检查，写report/state/handoff并刷新QUICKSTART，向用户汇报后停止。缺真实DS或账号如实partial，不把文件生成当上线。

本Skill不扩张权限、不自动启用付费或公开发布；遵循AGENTS和当前阶段。

## 4.3增量
4.3六项已正式批准，旧4.2工程先A18合并；真实代码状态与需求批准分开，QUICKSTART注明未完成落点。

## 4.4补充
4.4增量见CHANGELOG_4_4；已有4.3先A19保留真实状态，当前阶段写明策略配置与实际验证差异，刷新QUICKSTART后停止。

## 4.5当前覆盖
读specs/17与config/context_policy.json、behavior_policy.json。owner最高128K/普通最低32K；软作息允许夜间继续，明确停止/隐私/权限/8元总额和6元后台仍硬约束。后台沿用任务上下文，复用原链路，不新增模型或大量验证。

## 4.6增补
按需读specs/18与config/emotional_expression.json：可难过、生气、有脾气，不恒定积极。真实DS同回合、按人事件来源、可修正；不随机数值化、不让情绪改变控制/预算/权限。不新建常驻情绪模型或大测试，当前阶段完成后交接并停止。

## 4.7增补
4.7能力继续保留，当前4.8合并入口A23；P10/P11标题明确重点表达，P05首发不改。不得把已纳入需求或只通道能发当全部语音完成。

4.9：当前本机仅local_trial小批量，迁移策略见specs/24。按需安装/启动，少量真实DS/QQ/媒体样本；完成关闭项目临时进程，不自启/批量运行。主机路径与秘密分离，目标以后配置。已填房间I:\cuteroom；默认文字、语音仅明确要求且不付费。
