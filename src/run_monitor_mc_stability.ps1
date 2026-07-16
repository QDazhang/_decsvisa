param(
    [double]$McSetpointK = 0.006,
    [double[]]$PowersMw = @(1, 2, 3, 4),
    [double]$SamplePeriod = 75,
    [double]$WindowMinutes = 15,
    [double]$MaxStepMinutes = 60,
    [double]$StabilityPercent = 0.5,
    [double]$StabilitySlopeMkPerMin = 0.02,
    [bool]$StartServer = $true,
    [bool]$ShutdownServer = $true,
    [string]$OutputCsv
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))
$pythonExe = Join-Path $workspaceRoot "python-3.11.8.amd64\python.exe"
$monitorScript = Join-Path $scriptDir "monitor_mc_stability.py"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python interpreter not found at $pythonExe"
}

if (-not (Test-Path -LiteralPath $monitorScript)) {
    throw "Monitor script not found at $monitorScript"
}

$arguments = @(
    $monitorScript
    "--mc-setpoint-k", $McSetpointK
    "--sample-period", $SamplePeriod
    "--window-minutes", $WindowMinutes
    "--max-step-minutes", $MaxStepMinutes
    "--stability-percent", $StabilityPercent
    "--stability-slope-mk-per-min", $StabilitySlopeMkPerMin
    "--powers-mw"
)

$arguments += $PowersMw | ForEach-Object { $_.ToString() }

if ($StartServer) {
    $arguments += "--start-server"
}

if ($ShutdownServer) {
    $arguments += "--shutdown-server"
}

if ($OutputCsv) {
    $arguments += "--output-csv"
    $arguments += $OutputCsv
}

Write-Host "Running MC stability monitor..."
Write-Host "MC setpoint (K): $McSetpointK"
Write-Host "Still heater steps after initial 0 mW: $($PowersMw -join ', ')"

& $pythonExe @arguments
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "monitor_mc_stability.py exited with code $exitCode"
}
