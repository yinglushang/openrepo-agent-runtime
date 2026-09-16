# Agent Runtime 性能基线

## 结果

2026-09-16 在 Windows 11、Python 3.12.14、24 逻辑处理器的本地环境中执行基准测试，
原始结果为 `PASS`：

| 指标 | 结果 | 样本与延迟 |
| --- | ---: | --- |
| 任务完成率 | 100% | 30/30；p50 72.125 ms，p95 144.074 ms |
| 工具调用成功率 | 100% | 30/30 |
| 审批拒绝后恢复成功率 | 100% | 20/20；p50 55.023 ms，p95 57.951 ms |
| SSE 事件送达率 | 100% | 100/100；p50 3.242 ms，p95 3.531 ms |
| 审计日志写入吞吐 | 4,632.34 条/秒 | 单写入者、1,000 条；验链 14.340 ms |
| 运行时重启恢复成功率 | 100% | 10/10；p50 71.562 ms，p95 85.698 ms |

原始 Markdown/JSON 报告保留在本地，不随仓库上传。
下述复现命令会在 `artifacts/benchmarks/` 生成新的独立报告；不同机器的性能结果可能不同。

## 复现

安装开发依赖后执行：

```powershell
.venv\Scripts\python.exe -m scripts.benchmark
```

默认样本量为 30 个任务、20 次审批恢复、100 条 SSE 事件、1,000 条审计写入和 10 次
运行时重启恢复。可通过 `--tasks`、`--approvals`、`--sse-events`、`--audit-records` 和
`--restarts` 调整；每次执行都会创建独立的 Markdown 与 JSON 报告。

## 指标口径与边界

- 任务使用确定性脚本模型且每个任务执行一次 `read_file`，用于隔离 Agent Runtime 自身开销，
  不包含外部大模型和公网延迟。
- SSE 延迟从 `AgentRuntime.emit` 调用开始计时，包含 SQLite 事件持久化、Broker 分发、HTTP SSE
  序列化以及单个本机客户端收到事件的时间。
- 审批恢复延迟指“拒绝审批”到 LangGraph 从持久化 interrupt 恢复并完成任务的耗时。
- 重启恢复会关闭并重新创建 `AgentRuntime`，复用同一 SQLite 与 checkpoint 文件；这是服务运行时
  重启路径测试，不是操作系统重启或容灾测试。
- 审计链要求严格顺序，因此吞吐量按单写入者测量。所有结果都是当前机器上的本地基线，不能当作
  生产 SLA 或跨机器通用结论。
- 真实 `qwen-plus` 的 3 个任务验收见 [QWEN_ACCEPTANCE_RESULT.md](QWEN_ACCEPTANCE_RESULT.md)；
  该小样本用于证明真实模型接入，不与确定性性能基线混合统计。
- 44/44 表示固定策略回归集上的 Action/Rule 一致性，不表示任意危险命令识别准确率为 100%。
