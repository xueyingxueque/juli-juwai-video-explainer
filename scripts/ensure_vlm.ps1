param(
    [switch]$Quiet
)
# VLM watchdog. ASCII-only on purpose: PowerShell 5.1 reads .ps1 as GBK,
# so any Chinese char in this file breaks the parser.
$ErrorActionPreference = 'Stop'

$skillRoot = Split-Path -Parent $PSScriptRoot
$cfg = Join-Path $skillRoot 'config\engine.json'
$llama = 'D:\AITools\VLM\bin\llama-server.exe'
$model = 'D:\AITools\VLM\models\qwen3-vl-8b\Qwen3VL-8B-Instruct-Q4_K_M.gguf'
$mmproj = 'D:\AITools\VLM\models\qwen3-vl-8b\mmproj-Qwen3VL-8B-Instruct-F16.gguf'
$endpoint = 'http://127.0.0.1:18088'

if (Test-Path -LiteralPath $cfg) {
    $c = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
    $llama = $c.runtime.llama_server
    $model = $c.runtime.vlm_model
    $mmproj = $c.runtime.mmproj
    $endpoint = $c.runtime.vlm_endpoint
}

$port = ([uri]$endpoint).Port
$healthUrl = $endpoint + '/health'

function Test-Healthy {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -TimeoutSec 6 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (Test-Healthy) {
    if (-not $Quiet) { Write-Output 'VLM already running.' }
    exit 0
}

$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    if (-not $Quiet) {
        Write-Output ("Port " + $port + " occupied by PID " + $busy[0].OwningProcess + " but unhealthy.")
        Write-Output "Kill it manually if stale, then retry."
    }
    exit 2
}

if (-not (Test-Path -LiteralPath $llama)) { Write-Error ("llama-server missing: " + $llama) }
foreach ($x in @($model, $mmproj)) {
    if (-not (Test-Path -LiteralPath $x)) { Write-Error ("model file missing: " + $x) }
}

if (-not $Quiet) { Write-Output 'Starting VLM ...' }
$proc = Start-Process -FilePath $llama -PassThru -WindowStyle Hidden -ArgumentList @(
    '-m', $model, '--mmproj', $mmproj, '--host', '127.0.0.1',
    '--port', "$port", '-c', '16384', '-np', '1', '-ngl', '0')

$deadline = (Get-Date).AddSeconds(150)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 5
    if (Test-Healthy) {
        if (-not $Quiet) { Write-Output ('VLM ready. PID=' + $proc.Id) }
        exit 0
    }
    if ($proc.HasExited) {
        if (-not $Quiet) { Write-Output ('Process exited, code=' + $proc.ExitCode) }
        exit 1
    }
}

if (-not $Quiet) { Write-Output ('Timeout. PID=' + $proc.Id + ' -- check free memory (~6GB needed).') }
exit 1
