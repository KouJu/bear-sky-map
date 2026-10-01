# bear-sky-map 起動スクリプト
# 使い方: PowerShellでこのファイルのあるフォルダに移動して .\start-app.ps1
# (初回に実行ポリシーのエラーが出る場合は下記コマンドを一度だけ実行:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned )

$frontend = Join-Path $PSScriptRoot "frontend"

# 3秒後にブラウザを開く(サーバー起動を待つため)
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 3
    Start-Process "http://localhost:5173"
} | Out-Null

Write-Host "dev server を起動します (終了は Ctrl+C)..." -ForegroundColor Cyan
Set-Location $frontend
npm run dev
