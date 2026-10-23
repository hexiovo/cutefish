---
name: deepseek-live-calibration
description: 搭建DeepSeek实聊台、人格访谈、风格试聊和反馈时使用；禁止Codex代演。
---

# deepseek-live-calibration

读specs/10与04。真实DS API产生问题和角色回复，用户直接在本地UI回答；同一ChatService和预算用于QQ。记录响应来源、模型/配置版本、usage，raw content不经Codex润色。试聊命名空间独立，用户批准具体人格版本后启用。

本Skill不扩张权限、不自动启用付费或公开发布；遵循AGENTS和当前阶段。

## 4.2
按specs/13用实际情境和经历塑造同一角色，不制作好感度/情绪滑条或固定等待问卷。兴趣可以发展但不能越权；Codex不代演。

## 4.3增量
同一轮真实DS按specs/14落实交流意图、共同语境与误会修复；示例是主题不是实测台词，不增加独立评分或每轮分类。

## 4.4补充
4.4由同一PromptBuilder按可信身份应用owner128K/普通32K，后台沿用32K/64K/128K，扣输出余量；实际模型和tokenizer核实。前缀缓存不扩窗口，真实图片观察不可Codex代写。

## 4.5当前覆盖
读specs/17与config/context_policy.json、behavior_policy.json。owner最高128K/普通最低32K；软作息允许夜间继续，明确停止/隐私/权限/8元总额和6元后台仍硬约束。后台沿用任务上下文，复用原链路，不新增模型或大量验证。

## 4.6增补
按需读specs/18与config/emotional_expression.json：可难过、生气、有脾气，不恒定积极。真实DS同回合、按人事件来源、可修正；不随机数值化、不让情绪改变控制/预算/权限。不新建常驻情绪模型或大测试，当前阶段完成后交接并停止。

## 4.7增补
读specs/19；C01—C05以真实DS同次生成试聊，整段接话而非逐句必答；虚构与事实隔离，情绪针对事件，停止不能当反话。

4.9：当前本机仅local_trial小批量，迁移策略见specs/24。按需安装/启动，少量真实DS/QQ/媒体样本；完成关闭项目临时进程，不自启/批量运行。主机路径与秘密分离，目标以后配置。已填房间I:\cuteroom；默认文字、语音仅明确要求且不付费。
