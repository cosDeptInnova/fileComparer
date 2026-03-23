
param(
  [int]$CompDocsWorkerConcurrency = 0,
  [int]$CompDocsWorkerCount = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")

$config = Get-Config

Set-CompDocsWorkerScaling -Config $config -WorkerConcurrency $CompDocsWorkerConcurrency -WorkerCount $CompDocsWorkerCount

Start-AllServices -Config $config
Status-AllServices -Config $config
