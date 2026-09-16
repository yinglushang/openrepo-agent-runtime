[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$BaseUri = "http://127.0.0.1:8000",
    [int]$TimeoutSeconds = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$outputRoot = Join-Path $root "artifacts/docker-validation"

function Invoke-DockerCommand {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Wait-RuntimeHealth {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$BaseUri/health" -TimeoutSec 2
            if ($health.status -eq "ok") {
                return $health
            }
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    throw "Runtime did not become healthy within $TimeoutSeconds seconds"
}

Push-Location $root
try {
    $env:AGENT_PROVIDER = "demo"
    $env:AGENT_MODEL = "demo-safe"
    $env:AGENT_API_KEY = ""
    $env:AGENT_BASE_URL = ""

    $upArguments = @("compose", "up", "-d")
    if (-not $SkipBuild) {
        $upArguments += "--build"
    }
    Invoke-DockerCommand -Arguments $upArguments
    $initialHealth = Wait-RuntimeHealth

    $writeSession = Invoke-RestMethod -Method Post -Uri "$BaseUri/v1/sessions"
    $writeRun = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUri/v1/sessions/$($writeSession.id)/runs" `
        -ContentType "application/json" `
        -Body '{"prompt":"创建一个演示文件"}'

    $riskSession = Invoke-RestMethod -Method Post -Uri "$BaseUri/v1/sessions"
    $riskRun = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUri/v1/sessions/$($riskSession.id)/runs" `
        -ContentType "application/json" `
        -Body '{"prompt":"演示危险删除操作"}'

    $beforeContainer = (& docker compose ps -q agent-runtime).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $beforeContainer) {
        throw "Unable to resolve the initial container ID"
    }
    $fileBefore = (& docker compose exec -T agent-runtime python -c `
        "from pathlib import Path; p=Path('/app/workspace/demo-output.txt'); print(p.read_text(encoding='utf-8') if p.is_file() else 'MISSING')").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the validation file before recreation"
    }

    Invoke-DockerCommand -Arguments @("compose", "down")
    Invoke-DockerCommand -Arguments @("compose", "up", "-d", "--no-build")
    $recreatedHealth = Wait-RuntimeHealth

    $afterContainer = (& docker compose ps -q agent-runtime).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $afterContainer) {
        throw "Unable to resolve the recreated container ID"
    }
    $writeAfter = Invoke-RestMethod -Uri "$BaseUri/v1/sessions/$($writeSession.id)"
    $riskAfter = Invoke-RestMethod -Uri "$BaseUri/v1/sessions/$($riskSession.id)"
    $fileAfter = (& docker compose exec -T agent-runtime python -c `
        "from pathlib import Path; p=Path('/app/workspace/demo-output.txt'); print(p.read_text(encoding='utf-8') if p.is_file() else 'MISSING')").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read the validation file after recreation"
    }

    $resumed = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUri/v1/sessions/$($riskSession.id)/approvals" `
        -ContentType "application/json" `
        -Body '{"approved":false,"remember_for_run":false}'
    $audit = Invoke-RestMethod -Uri "$BaseUri/v1/audit/verify"

    $containerUser = (& docker compose exec -T agent-runtime id -u).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the container user"
    }
    $restartPolicy = (& docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' $afterContainer).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the restart policy"
    }

    $passed = (
        $initialHealth.status -eq "ok" -and
        $recreatedHealth.status -eq "ok" -and
        $beforeContainer -ne $afterContainer -and
        $writeRun.status -eq "completed" -and
        $riskRun.status -eq "awaiting_approval" -and
        $riskRun.pending_approval.tool_name -eq "run_command" -and
        $writeAfter.status -eq "completed" -and
        $riskAfter.status -eq "awaiting_approval" -and
        $fileBefore -eq "created by demo agent" -and
        $fileAfter -eq $fileBefore -and
        $resumed.status -eq "completed" -and
        $audit.valid -and
        $containerUser -eq "10001" -and
        $restartPolicy -eq "unless-stopped"
    )

    $report = [ordered]@{
        generated_at = (Get-Date).ToUniversalTime().ToString("o")
        passed = $passed
        provider = $recreatedHealth.provider
        model = $recreatedHealth.model
        before_container = $beforeContainer
        after_container = $afterContainer
        container_recreated = ($beforeContainer -ne $afterContainer)
        container_user_id = $containerUser
        restart_policy = $restartPolicy
        write_session_after_recreation = $writeAfter.status
        risk_session_after_recreation = $riskAfter.status
        approval_result = $resumed.status
        workspace_file_before = $fileBefore
        workspace_file_after = $fileAfter
        audit_valid = $audit.valid
        audit_records = $audit.records
    }

    $runDirectory = Join-Path $outputRoot (Get-Date -Format "yyyyMMddTHHmmss")
    New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
    $reportPath = Join-Path $runDirectory "report.json"
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
    $report | ConvertTo-Json -Compress
    "Docker validation report: $reportPath"

    if (-not $passed) {
        throw "Docker validation failed; inspect $reportPath"
    }
} finally {
    Pop-Location
}
