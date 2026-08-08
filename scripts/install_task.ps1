# 配置自动运行（普通 PowerShell 执行即可，无需管理员）：
#   1) 每天 15:30 定时任务（schtasks）
#   2) 登录时启动项（开机自动补跑，解决 15:30 没开机的情况）
$ErrorActionPreference = "Stop"

$pythonw = "C:\veighna_studio\pythonw.exe"
$script = "D:\下载的堆砌\vnpy-4.4.0\ai_demo\ai_trader\run.py"
$taskName = "AITrader"

if (-not (Test-Path $pythonw)) {
    Write-Host "[错误] 未找到 Python: $pythonw" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $script)) {
    Write-Host "[错误] 未找到脚本: $script" -ForegroundColor Red
    exit 1
}

# 1) 每天 15:30 定时任务（若已存在则覆盖）
schtasks /Create /TN $taskName /TR '"C:\veighna_studio\pythonw.exe" "D:\下载的堆砌\vnpy-4.4.0\ai_demo\ai_trader\run.py"' /SC DAILY /ST 15:30 /F

# 2) 登录时启动项（开机自动补跑）
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$startup\AITrader.lnk")
$lnk.TargetPath = $pythonw
$lnk.Arguments = '"D:\下载的堆砌\vnpy-4.4.0\ai_demo\ai_trader\run.py"'
$lnk.WorkingDirectory = "D:\下载的堆砌\vnpy-4.4.0\ai_demo\ai_trader"
$lnk.Description = "AI 交易每日自动运行（登录时）"
$lnk.Save()

Write-Host "[OK] 已配置：每天15:30定时任务 + 登录时自动运行（开机补跑）" -ForegroundColor Green
