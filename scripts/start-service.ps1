param(
  [Parameter(Mandatory=$true)][string]$Name,
  [int]$CompDocsWorkerConcurrency = 0,
  [int]$CompDocsWorkerCount = 0,
  [switch]$ShowStatus
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\common.ps1"
$config = Get-Config

Activate-CondaEnv -EnvName $config.CondaEnv

$svc = Resolve-ServiceByName -Config $config -Name $Name
$isCompDocsService = ((Try-GetProp -Obj $svc -Name "Name" -Default "") -ieq "comp_docs")
$isCompDocsWorker = ((Try-GetProp -Obj $svc -Name "Name" -Default "") -ieq "comp_docs_worker")

if ($isCompDocsService -or $isCompDocsWorker) {
  Set-CompDocsWorkerScaling -Config $config -WorkerConcurrency $CompDocsWorkerConcurrency -WorkerCount $CompDocsWorkerCount
}

Start-ServiceProcess -Config $config -Svc $svc

if ($ShowStatus) {
  Status-AllServices -Config $config
}