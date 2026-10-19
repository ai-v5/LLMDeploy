param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$RuntimeDir,
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$TaskName = 'LLMDeploy-Notifications'
)
$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$configPath = (Resolve-Path -LiteralPath $Config).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
if ([IO.Path]::GetFileName($pythonPath) -ine 'pythonw.exe') {
    throw 'Use pythonw.exe for the hidden background worker'
}
$runtimePath = [IO.Path]::GetFullPath($RuntimeDir)
if ((Test-Path -LiteralPath $runtimePath) -or (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw 'Runtime or scheduled task already exists; inspect it before changing it'
}
Push-Location -LiteralPath $sourceRoot
try {
    $consolePython = Join-Path (Split-Path -Parent $pythonPath) 'python.exe'
    & $consolePython -m deploy_notify --config $configPath --check
    if ($LASTEXITCODE -ne 0) { throw 'Invalid notification configuration' }
    $settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($null -eq $settings.backend) { throw 'No notification backend enabled' }
    foreach ($value in @($settings.backend_state, $settings.backend.token_file)) {
        if (-not [IO.Path]::IsPathRooted($value)) {
            throw 'Use absolute local paths when installing a runtime copy'
        }
    }
    New-Item -ItemType Directory -Path $runtimePath | Out-Null
    $packagePath = Join-Path $runtimePath 'deploy_notify'
    New-Item -ItemType Directory -Path $packagePath | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'deploy_notify') -Filter '*.py' -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $packagePath
    }
    $runtimeConfig = Join-Path $runtimePath 'config.json'
    # Resolve application-virtualized executable paths before Task Scheduler starts outside that app.
    & $consolePython -m deploy_notify --config $configPath --export-config $runtimeConfig
    if ($LASTEXITCODE -ne 0) { throw 'Runtime configuration export failed' }
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-m deploy_notify.watchdog --config "' + $runtimeConfig + '"') -WorkingDirectory $runtimePath
    $logon = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $logon.Delay = 'PT1H'
    $logon.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)).Repetition
    $options = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon -Settings $options -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Output 'Notification task installed; inspect alert-state.json for actual health and delivery status'
} finally {
    Pop-Location
}
