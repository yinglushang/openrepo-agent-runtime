# Docker Deployment Validation

本项目不仅提供 Dockerfile 和 Compose 配置，还对真实 Docker Desktop 环境执行容器构建、健康检查、
命名卷持久化和跨容器恢复验收。

## 已验证基线

- Date: `2026-09-16`
- Docker client / server: `29.7.2 / 29.7.2`
- Base image: `python:3.12-slim`
- Runtime image size: `79,562,084 bytes`（约 75.9 MiB）
- Container user: `agent`（UID `10001`）
- Restart policy: `unless-stopped`
- Health state: `healthy`
- Runtime provider: `demo / demo-safe`

## 验收结果

| Check | Result |
| --- | ---: |
| Image build and dependency installation | PASS |
| `/health` response after startup | PASS |
| Non-root runtime user | PASS |
| `/app/data` named volume mounted | PASS |
| `/app/workspace` named volume mounted | PASS |
| Container removed and recreated | PASS |
| Completed session restored from SQLite | PASS |
| Pending approval restored after recreation | PASS |
| Approval rejection resumed the LangGraph run | PASS |
| Workspace file survived recreation | PASS |
| SHA-256 audit chain verification | PASS |
| Runtime log `ERROR` / `Traceback` matches | 0 |

恢复测试创建了一个安全写文件任务和一个危险删除任务。删除 Compose 容器和网络但保留命名卷后，
新容器 ID 与原容器不同；已完成 session 仍为 `completed`，危险任务仍为 `awaiting_approval`，
`demo-output.txt` 内容保持不变。拒绝审批后任务恢复并进入 `completed`，审计链验证通过。

## 一键复现

在 Docker daemon 已运行且 `8000` 端口可用时执行：

```powershell
cd openrepo-agent-runtime
.\scripts\docker_validate.ps1
```

已存在最新镜像时可以跳过构建：

```powershell
.\scripts\docker_validate.ps1 -SkipBuild
```

脚本强制使用离线 Demo provider，不读取或注入真实模型 API Key；它不会执行 `docker compose down -v`，
因此不会删除命名卷。每次运行会在 `artifacts/docker-validation/` 下生成独立 JSON 报告。
