param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$InputPdf,
    [Parameter(Position = 1)]
    [string]$OutputDirectory = (Join-Path (Get-Location) 'translated'),
    [ValidateSet('Codex', 'Local')]
    [string]$Provider = 'Codex',
    [string]$Model = '',
    [ValidateSet('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$Effort = 'low',
    [string]$Glossary = '',
    [ValidateRange(1, 8)]
    [int]$Parallel = 8,
    # Speed tier is independent of effort and defaults to economical Standard.
    [ValidateSet('standard', 'fast')]
    [string]$ServiceTier = 'standard'
)

# Stop on setup errors and resolve paths relative to this installation.
$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $toolRoot '.venv\Scripts\python.exe'
$pdf2zhExe = Join-Path $toolRoot '.venv\Scripts\pdf2zh_next.exe'
if (-not (Test-Path -LiteralPath $InputPdf -PathType Leaf)) { throw "PDF not found: $InputPdf" }
$InputPdf = (Resolve-Path -LiteralPath $InputPdf).Path
if ([IO.Path]::GetExtension($InputPdf) -ine '.pdf') { throw 'Input must be a PDF.' }
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
# Each job has an independent failure report; recovered retries remove their entries.
$statusFile = Join-Path $OutputDirectory ('.codex-status-' + [guid]::NewGuid().ToString('N') + '.json')
$bridgeArguments = @()
if ($Provider -eq 'Codex') {
    # Require ChatGPT login explicitly; the bridge strips inherited API billing keys.
    $bridge = Join-Path $toolRoot 'codex_translate.py'
    & $pythonExe $bridge --check-login
    if ($LASTEXITCODE -ne 0) { throw 'Run codex login with your ChatGPT account first.' }
    # Resolve the account model explicitly so cache identity tracks the model actually used.
    Push-Location -LiteralPath $toolRoot
    try {
        $accountJson = & $pythonExe -c 'import json; from codex_account import fetch_account_snapshot; print(json.dumps(fetch_account_snapshot()))'
        if ($LASTEXITCODE -ne 0) { throw 'Could not read Codex model catalog.' }
        $account = $accountJson | ConvertFrom-Json
        if (-not $Model) { $Model = ($account.models | Where-Object isDefault | Select-Object -First 1).model }
        $selectedModel = $account.models | Where-Object model -eq $Model | Select-Object -First 1
        if (-not $selectedModel) { throw 'Select a model available in your Codex account.' }
        if ($Effort -notin @($selectedModel.supportedReasoningEfforts.reasoningEffort)) {
            throw "Model $Model does not support effort $Effort. Choose a supported effort."
        }
    } finally { Pop-Location }
    $documentHash = (Get-FileHash -LiteralPath $InputPdf -Algorithm SHA256).Hash.ToLowerInvariant()
    $bridgeArguments = @('--document', $documentHash, '--effort', $Effort, '--status-file', $statusFile)
    # Forward the requested tier to batch and legacy Codex calls.
    $bridgeArguments += @('--service-tier', $ServiceTier)
    # Independent parallel turns use no completion-order-dependent previous translations.
    $contextCharacters = '0'
    $bridgeArguments += @('--max-chars', '4000', '--context-chars', $contextCharacters)
    if ($Model) { $bridgeArguments += @('--model', $Model) }
    if ($Glossary) {
        $Glossary = (Resolve-Path -LiteralPath $Glossary).Path
        $bridgeArguments += @('--glossary', $Glossary)
    }
} else {
    # Keep a single local worker to avoid simultaneous loads of a large GPU model.
    $Parallel = 1
    # Reuse the same local model bridge and Traditional Chinese policy as the desktop GUI.
    $bridge = Join-Path $toolRoot 'qwen36_translate.py'
    if (-not $Model) { $Model = 'qwen3.8:27b-q4_K_M' }
    # 使用使用者自己的 Ollama 安裝，不依賴原作者的資料夾。
    $ollamaExe = (Get-Command ollama -ErrorAction Stop).Source
    try {
        Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
    } catch {
        Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
        # Poll readiness rather than assuming a fixed startup delay is sufficient.
        $ready = $false
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            Start-Sleep -Seconds 1
            try {
                Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            } catch {}
        }
        if (-not $ready) { throw 'Ollama did not become ready.' }
    }
    $bridgeArguments = @('--model', $Model, '--effort', 'none')
}

# Use Python shlex.join because pdf2zh parses POSIX-style quotes, including paths with spaces.
$bridgeParts = @($pythonExe.Replace('\', '/'), $bridge.Replace('\', '/')) + $bridgeArguments
$bridgeJson = ConvertTo-Json -InputObject $bridgeParts -Compress
$bridgeCommand = $bridgeJson | & $pythonExe -c 'import json,shlex,sys; print(shlex.join(json.load(sys.stdin)))'
if ($LASTEXITCODE -ne 0) { throw 'Could not construct translator command.' }

# Parallel Codex turns have bounded independent context; glossary/rules are shared consistently.
# Use the same original-glyph protection as both desktop entry points.
$protectedRunner = Join-Path $toolRoot 'protected_translate.py'
$cacheOptions = @()
# Let the versioned Codex bridge control reuse after translation-policy changes.
if ($Provider -eq 'Codex') { $cacheOptions = @('--ignore-cache') }
& $pythonExe $protectedRunner $InputPdf --output $OutputDirectory --clitranslator `
    --clitranslator-command $bridgeCommand --clitranslator-timeout 1800 `
    --lang-in en --lang-out zh-TW --qps $Parallel --pool-max-workers $Parallel `
    --no-auto-extract-glossary --watermark-output-mode no_watermark `
    --no-remove-non-formula-lines --disable-config-auto-save @cacheOptions
$translationExit = $LASTEXITCODE
# Upstream can return success despite failed paragraphs; explicitly check the bridge report.
if ($Provider -eq 'Codex' -and (Test-Path -LiteralPath $statusFile)) {
    $failures = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (@($failures.PSObject.Properties).Count -gt 0) {
        Write-Warning "Some Codex segments failed. The output PDF may contain untranslated text. Details: $statusFile"
        exit 1
    }
    # Verify the report is a direct child of this job's output directory before removing it.
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($statusFile)) -ne $OutputDirectory) {
        throw 'Unsafe status file path.'
    }
    Remove-Item -LiteralPath $statusFile
}
# Require affirmative batch completion because upstream may silently fall back to source text.
if ($Provider -eq 'Codex' -and $env:PDF_TRANSLATE_LEGACY -ne '1') {
    $completionFile = [IO.Path]::ChangeExtension($statusFile, '.complete.json')
    if (-not (Test-Path -LiteralPath $completionFile)) { throw 'Codex batch translation did not complete.' }
    $completion = Get-Content -LiteralPath $completionFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($completion.success -ne $true) { throw 'Codex batch translation was not validated.' }
    Remove-Item -LiteralPath $completionFile
}
exit $translationExit
