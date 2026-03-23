param(
  [string[]]$Queue = @(),
  [switch]$Burst
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

if (-not $env:COMPARE_WINDOWS_WORKER_MODE) {
  $env:COMPARE_WINDOWS_WORKER_MODE = "production"
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

Write-Host "[comp_docs_worker] Windows detected. DO NOT use 'rq worker' here; launching python $($argsList -join ' ')" -ForegroundColor Cyan
& python @argsList