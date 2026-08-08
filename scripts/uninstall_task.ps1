# 卸载自动运行配置：删除定时任务 + 登录启动项
$taskName = "AITrader"
schtasks /Delete /TN $taskName /F 2>$null
$startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
Remove-Item "$startup\AITrader.lnk" -ErrorAction SilentlyContinue
Write-Host "[OK] 已移除定时任务与登录启动项"
