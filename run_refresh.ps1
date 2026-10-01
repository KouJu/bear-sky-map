# bear-sky-map データ定期更新スクリプト
#
# 手動実行:  .\run_refresh.ps1
# タスクスケジューラ登録例 (毎朝6時):
#   プログラム: powershell.exe
#   引数: -NoProfile -ExecutionPolicy Bypass -File "C:\ClaudeCode\bear-sky-map\run_refresh.ps1"
#
# 実行結果は logs\ に日付付きで保存され、Discord にも通知されます
# (backend\discord_webhook.txt が設定されている場合)。

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$python = Join-Path $root "backend\venv\Scripts\python.exe"
$script = Join-Path $root "backend\refresh_data.py"
$logDir = Join-Path $root "logs"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("refresh_{0:yyyy-MM-dd_HHmmss}.log" -f (Get-Date))

Write-Host "更新を開始します。ログ: $log" -ForegroundColor Cyan

# 標準出力・標準エラーの両方をログに残しつつ画面にも表示
& $python $script *>&1 | Tee-Object -FilePath $log
$exitCode = $LASTEXITCODE

# ログは直近30件だけ残す
Get-ChildItem $logDir -Filter "refresh_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

if ($exitCode -eq 0) {
    Write-Host "更新が正常に完了しました。" -ForegroundColor Green
} else {
    Write-Host "更新中にエラーが発生しました。ログを確認してください: $log" -ForegroundColor Red
}
exit $exitCode
