# 建立獨立環境；不複製其他電腦的文件、登入資料或金鑰。
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '請先安裝 Python 3.12（包含 Python Launcher）。' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '套件安裝失敗。' }
Write-Host '安裝完成。Codex 使用者請安裝官方 CLI 並執行 codex login，然後雙擊 Start-翻譯GUI.cmd。'
