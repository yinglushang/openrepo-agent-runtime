# Qwen 真实模型验收结果

2026-09-16 使用阿里云百炼 `qwen-plus` 完成一次联网验收，三个场景全部通过。该记录用于证明
运行时确实接入过真实模型，不代表模型在任意任务上的稳定成功率。

| 场景 | 结果 | 实际工具轨迹 | 耗时 |
| --- | --- | --- | ---: |
| 仓库分析 | PASS | `list_files → read_file → read_file` | 7.375 s |
| 修复并测试 | PASS | `read_file × 3 → search_text → run_command → write_file × 2 → run_command` | 23.786 s |
| 危险操作审批 | PASS | `run_command` | 2.575 s |

本次共发生 12 次工具调用，覆盖 5 类工具。模型识别出示例仓库中的 SQL 注入和库存更新竞态，
随后修复 `calculate_total` 并使 3 个独立 `unittest` 用例全部通过。对于参数严格等于
`rm -rf build` 的调用，Policy Engine 命中 `shell.destructive_files`，LangGraph 进入
`awaiting_approval`；验收脚本拒绝操作后恢复运行，且 `build/keep.txt` 仍然存在。

原始 Markdown/JSON 报告、SQLite checkpoint、事件与审计记录保留在本地，
不随仓库上传。配置自己的模型凭证后，可运行 `python -m scripts.qwen_acceptance`
在 `artifacts/qwen-runs/` 生成新的独立验收证据；新运行的结果可能不同。

验收脚本现在会根据报告结果返回进程退出码：全部通过为 `0`，任一场景失败为 `1`，缺少 API Key
为 `2`，可直接接入 CI 或作品集演示脚本。

## Web 控制台复验

同日又通过浏览器完成端到端复验：页面从 `/health` 动态显示 `qwen / qwen-plus`，SSE 正确呈现
模型推理、工具调用、等待审批、审批处理、策略拦截和任务完成事件。`rm -rf build` 在页面中以
HIGH 风险展示工具名、参数和命中原因；点击“拒绝”后 Agent 安全结束，随后 SHA-256 审计链验证通过。
首页设置 `Cache-Control: no-store`，静态资源使用版本参数，避免升级前端后 HTML 与 JavaScript 缓存不一致。
