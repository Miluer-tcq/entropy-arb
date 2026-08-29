# run-live.ps1 — start the live bot and auto-restart it if it crashes.
# run-live.ps1 —— 启动实盘并在进程崩溃后自动重启。
#
# Usage / 用法:
#   .\run-live.ps1                                  # SNDK x Lighter, config-lighter.yaml
#   .\run-live.ps1 -Config config-rh.yaml -Hedge lighter-rh
#   .\run-live.ps1 -ExtraArgs @('--record-only')    # collectors, no orders
#   .\run-live.ps1 -NoPreflight                     # skip the pre-start check
#
# Stop / 停止:  Ctrl+C in this window (clean stop, no restart)
#              or close the window. A second bot with the same config is
#              refused by the instance lock either way.
param(
    [string]$Config = "config-lighter.yaml",
    [string]$Symbol = "SNDK",
    [string]$Hedge = "lighter",
    [string[]]$ExtraArgs = @(),
    [switch]$NoPreflight,
    [int]$RestartDelaySec = 5,
    [switch]$Cn
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) { $py = "python" }

$baseArgs = @("main.py", "--symbol", $Symbol, "--hedge", $Hedge,
              "--config", $Config) + $ExtraArgs
if ($Cn) { $baseArgs += "--cn" }

if (-not $NoPreflight) {
    & $py ($baseArgs + @("--preflight"))
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "preflight FAILED (exit $LASTEXITCODE) — not starting. Fix the red items above." -ForegroundColor Red
        Write-Host "预检未通过（退出码 $LASTEXITCODE）——不启动。请先修复上方红色项。" -ForegroundColor Red
        exit 1
    }
}

# exit codes that mean "don't restart" / 不应重启的退出码:
#   0            clean stop (Ctrl+C handled by the bot) / 正常停止
#   2            config error or another instance holds the lock / 配置错误或实例锁
#   -1073741510  0xC000013A: console Ctrl+C / 控制台 Ctrl+C
while ($true) {
    & $py $baseArgs
    $rc = $LASTEXITCODE
    if ($rc -eq 0 -or $rc -eq 2 -or $rc -eq -1073741510) {
        Write-Host "engine stopped (exit $rc) — not restarting." -ForegroundColor Cyan
        exit $rc
    }
    Write-Host "engine exited with $rc — restarting in ${RestartDelaySec}s (Ctrl+C to abort)" -ForegroundColor Yellow
    Start-Sleep -Seconds $RestartDelaySec
}
