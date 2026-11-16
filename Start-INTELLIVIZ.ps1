$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms

$bundledPython = Join-Path $PSScriptRoot 'runtime\python.exe'
$standalone = Test-Path -LiteralPath $bundledPython -PathType Leaf
$port = if ($standalone) { 18501 } else { 8501 }
$appUrl = "http://127.0.0.1:$port"
$workspaceUrl = "$appUrl/?workspace=aea4a66e854bdaf9c496415ef7ad90e9"
$profile = Join-Path $env:LOCALAPPDATA 'INTELLIVIZ\DesktopProfile'
$mutex = [Threading.Mutex]::new($false, 'Local\INTELLIVIZ-Desktop-Launcher')
$locked = $false
$server = $null
$source = $PSScriptRoot
if ($standalone) {
    $sourceLink = Join-Path $PSScriptRoot 'shared-source.txt'
    if (Test-Path -LiteralPath $sourceLink) {
        $linkedSource = (Get-Content -LiteralPath $sourceLink -Raw -Encoding UTF8).Trim()
        if (Test-Path -LiteralPath (Join-Path $linkedSource 'app.py') -PathType Leaf) {
            $source = $linkedSource
        }
    }
}

function Test-Dashboard {
    try {
        $response = Invoke-WebRequest -UseBasicParsing "$appUrl/_stcore/health" -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content.Trim() -eq 'ok'
    } catch {
        return $false
    }
}

try {
    try {
        $locked = $mutex.WaitOne(45000)
    } catch [Threading.AbandonedMutexException] {
        $locked = $true
    }
    if (-not $locked) { throw 'INTELLIVIZ is already starting. Try again shortly.' }

    $edge = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $edge) { throw 'Microsoft Edge is required to open the INTELLIVIZ desktop window.' }

    if (-not (Test-Dashboard)) {
        $python = if ($standalone) { $bundledPython } else { (Get-Command python.exe -CommandType Application).Source }
        if ($standalone) {
            $env:FL511_FFMPEG = Join-Path $PSScriptRoot 'runtime\ffmpeg.exe'
            $env:FL511_DATA_DIR = Join-Path $env:LOCALAPPDATA 'INTELLIVIZ\Data'
        }
        $server = Start-Process -FilePath $python -WorkingDirectory $source -WindowStyle Hidden -PassThru -ArgumentList @(
            '-m', 'streamlit', 'run', 'app.py', '--server.address', '127.0.0.1',
            '--server.port', "$port", '--server.headless', 'true'
        )
        $deadline = [DateTime]::UtcNow.AddSeconds(35)
        while (-not (Test-Dashboard)) {
            if ($server.HasExited -or [DateTime]::UtcNow -ge $deadline) {
                throw "The local dashboard engine could not start on port $port."
            }
            Start-Sleep -Milliseconds 300
        }
    }

    Start-Process -FilePath $edge -ArgumentList @("--app=$workspaceUrl", "--user-data-dir=`"$profile`"")
} catch {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -ErrorAction SilentlyContinue }
    [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'INTELLIVIZ')
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
