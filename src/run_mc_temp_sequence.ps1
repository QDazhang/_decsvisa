param(
    [Parameter(Mandatory = $true)]
    [string]$McSetpointsK,
    [double]$StillPowerMw = 4,
    [double]$SamplePeriod = 15,
    [bool]$StartServer = $true,
    [bool]$ShutdownServer = $true,
    [string]$OutputCsv
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $scriptDir))
$pythonExe = Join-Path $workspaceRoot "python-3.11.8.amd64\python.exe"
$sequenceScript = Join-Path $scriptDir "mc_temp_sequence.py"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python interpreter not found at $pythonExe"
}

if (-not (Test-Path -LiteralPath $sequenceScript)) {
    throw "Sequence script not found at $sequenceScript"
}

$parsedSetpoints = @()
foreach ($piece in ($McSetpointsK -split ",")) {
    $trimmed = $piece.Trim()
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        continue
    }

    $parsedValue = 0.0
    if (-not [double]::TryParse($trimmed, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsedValue)) {
        throw "Could not parse MC setpoint value '$trimmed' as a floating-point number."
    }
    $parsedSetpoints += $parsedValue
}

if ($parsedSetpoints.Count -eq 0) {
    throw "At least one MC setpoint is required."
}

$arguments = @(
    $sequenceScript
    "--mc-setpoints-k"
)

$arguments += $parsedSetpoints | ForEach-Object { $_.ToString([System.Globalization.CultureInfo]::InvariantCulture) }

$arguments += @(
    "--still-power-mw", $StillPowerMw
    "--sample-period", $SamplePeriod
)

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

Write-Host "Running MC temperature sequence..."
Write-Host "MC setpoint steps (K): $($parsedSetpoints -join ', ')"
Write-Host "Still heater power (mW): $StillPowerMw"
Write-Host "Sample period (s): $SamplePeriod"

& $pythonExe @arguments
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    throw "mc_temp_sequence.py exited with code $exitCode"
}
