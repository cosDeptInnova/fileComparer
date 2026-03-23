param(
  [string[]]$Queue = @(),
  [switch]$Burst,
  [int]$Concurrency = 0,
  [string]$Pool = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

if (-not $env:PYTHONPATH) {
  $env:PYTHONPATH = $repoRoot
} elseif (-not ($env:PYTHONPATH -split [IO.Path]::PathSeparator | Where-Object { $_ -eq $repoRoot })) {
  $env:PYTHONPATH = "$($env:PYTHONPATH)$([IO.Path]::PathSeparator)$repoRoot"
}

$argsList = @("-m", "app.worker")
if ($Queue.Count -gt 0) {
  foreach ($queueName in $Queue) {
    if ($queueName) { $argsList += @("--queue", $queueName) }
  }
} else {
  $argsList += @("--queue", $(if ($env:COMPARE_QUEUE_NAME) { $env:COMPARE_QUEUE_NAME } else { "compare" }))
}
if ($Burst) { $argsList += "--burst" }
if ($Concurrency -gt 0) { $argsList += @("--concurrency", [string]$Concurrency) }
if ($Pool) { $argsList += @("--pool", $Pool) }

Write-Host "[comp_docs_worker] launching Celery worker via python $($argsList -join ' ')" -ForegroundColor Cyan
& python @argsList
