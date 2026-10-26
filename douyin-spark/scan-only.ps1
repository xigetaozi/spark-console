# scan-only.ps1 —— 本机唯一用途：扫码导出登录态
# 不会启动 Web 服务，不会绑定任何端口，不会发送任何消息。
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "虚拟环境缺失。请先在本目录执行：" -ForegroundColor Red
    Write-Host "  python -m venv .venv"
    Write-Host "  .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    Write-Host "  .\.venv\Scripts\python.exe -m playwright install chromium"
    exit 1
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "  仅扫码模式（不会启动服务、不会发消息）" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "即将弹出浏览器，请用手机抖音 App 扫码登录。" -ForegroundColor Yellow
Write-Host "登录成功后脚本会自动保存 data\state.json 并关闭浏览器。" -ForegroundColor Yellow
Write-Host ""

& $py .\extract_cookie.py
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0 -and (Test-Path ".\data\state.json")) {
    Write-Host "✔ 登录态已生成：$PWD\data\state.json" -ForegroundColor Green
    Write-Host "  下一步：把它上传到你的服务器面板（http://你的服务器IP:8000）" -ForegroundColor Green
} else {
    Write-Host "✘ 未生成登录态（扫码超时或被取消）。可重新运行本脚本。" -ForegroundColor Red
}
Write-Host ""
Read-Host "按回车关闭窗口"
