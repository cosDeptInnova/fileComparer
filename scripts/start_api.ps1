param(
  [switch]$Reload
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

$argsList = @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8007")
if ($Reload) { $argsList += "--reload" }

Write-Host "[comp_docs_api] launching python $($argsList -join ' ')" -ForegroundColor Cyan
& python @argsList