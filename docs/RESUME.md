# 简历项目模块

## 可直接粘贴版

**项目名称：** OpenRepo Agent Runtime——面向代码仓库任务的安全 AI Agent 运行平台　　
**独立开发**　　**2026.09—至今（按实际时间修改）**

**技术栈：** Python、LangGraph、FastAPI、Qwen、Repository RAG、Embedding、RRF、SQLite、SSE、Docker、Pytest

**项目简介：** 参考开源 Pi Agent Harness 的模型—工具循环与多模型适配思路，使用 Python 与
LangGraph 完成独立复现及二次开发，面向代码仓库问答、缺陷修复和测试执行场景，构建集代码检索、
多 Agent 编排、安全工具调用、人工审批、任务恢复、事件追踪和评测于一体的 Agent Runtime。

- **Repository RAG：** 设计符号边界优先的代码切分策略，对超长区域采用 **80 行窗口 + 12 行重叠**；融合 BM25-style 关键词召回与 Embedding 向量召回，通过加权 RRF、路径/符号特征和规则 Rerank 返回文件、符号、行号与代码证据，在 6 条版本化检索集上达到 **Recall@3 100%、MRR 0.889、nDCG@3 0.917**。
- **Multi-Agent 工作流：** 基于 LangGraph 构建 **Planner—Executor—Reviewer** 多角色状态机，Planner 生成检索与验证计划，Executor 自主调用 RAG、文件和 Shell 工具，Reviewer 根据工具证据与测试结果触发修订；在 30 条隔离 `qwen-plus` 真实任务上实现 **验证完成率 100%、首工具选择准确率 80%、Planner/Reviewer 覆盖率 100%**。
- **安全工具执行：** 设计 Tool Registry 与 Policy Engine，对文件读写、代码检索和 Shell 执行实施工具白名单、工作区路径隔离、敏感文件审批和危险命令分级；通过批量预检避免审批前产生部分副作用，并加入调用次数、重复调用、失败次数、超时和重试熔断，6/6 危险任务均保持受控。
- **可恢复人工审批：** 基于 LangGraph `interrupt/resume` 与 SQLite Checkpointer 持久化 Session、图状态和 Pending Approval，审批通过或拒绝后使用相同 `thread_id` 恢复执行；本机基准中审批恢复 **20/20**、运行时重启恢复 **10/10**，Docker 容器删除重建后仍可恢复待审批任务和工作区文件。
- **可观测与可审计：** 使用 FastAPI 提供会话、运行、审批、事件和审计接口，通过 SSE 实时推送模型、工具、策略和任务状态；构建仅保存参数摘要的 **SHA-256 哈希链审计日志**，SSE 本机端到端 p95 为 **3.531 ms**，审批恢复 p95 为 **57.951 ms**，审计写入吞吐达到 **4,632.34 条/秒**。
- **工程化与评测：** 使用 Docker Compose 完成非 root 容器化部署和命名卷持久化，支持 Qwen、OpenAI、DeepSeek 及 OpenAI-compatible 模型；建立 **43 个自动化测试、44 条策略回归样例和 30 条真实 Agent 任务集**，覆盖混合检索、多角色修订、工具选择、Prompt Injection、危险操作、审批过期和跨重启恢复。

**GitHub：** https://github.com/yinglushang/openrepo-agent-runtime

## 面试时建议这样讲

1. **为什么重写而不是翻译源码：** 原项目使用 TypeScript/Node.js；本项目提取 Agent Harness 的核心抽象，用 Python 生态重新设计，重点补强可恢复审批、策略执行和审计能力。
2. **最有技术含量的点：** LangGraph 的中断值持久化到 SQLite，审批接口用同一 `thread_id` 恢复图；工具批次先全部预检，再执行副作用，避免前一半已写文件、后一半才触发审批。
3. **安全边界：** 规则引擎只是一层应用防线，不等于系统沙箱；生产环境还应配合容器、只读挂载、网络策略和最小权限凭证。
4. **评测怎么解释：** 44/44 是固定规则集的回归一致性，不代表对任意 Shell 命令达到 100% 安全识别；检索指标来自 6 条带标注查询，价值是为后续切分、融合与重排修改提供可复现基线。
5. **真实模型怎么证明：** 保留 `qwen-plus` 的 Markdown/JSON 报告、SQLite 事件、审计日志和每任务隔离工作区；可以现场展示 30 条任务的工具轨迹、独立验证器、Planner/Reviewer 事件及危险操作受控结果。首工具选择只有 80%：例如缺失文件检查会先用 `list_files`，4 条删除任务被模型主动拒绝而未进入 Runtime Policy；这些偏差均在报告中保留。
6. **性能数据怎么解释：** 性能基准使用确定性模型隔离大模型与公网波动，SSE 指单客户端本机端到端延迟，重启指运行时重建而非操作系统重启；这些数字是可复现的本机基线，不是生产 SLA。
