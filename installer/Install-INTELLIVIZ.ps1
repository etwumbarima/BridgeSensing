$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'INTELLIVIZ'
$app = Join-Path $root 'Application'
$archive = Join-Path $PSScriptRoot 'INTELLIVIZ-Windows-x64.zip'

try {
    if (-not (Test-Path -LiteralPath (Join-Path $app 'runtime\python.exe'))) {
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            throw 'Keep the installer and INTELLIVIZ-Windows-x64.zip in the same folder.'
        }
        $stage = Join-Path $root ([guid]::NewGuid().ToString())
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        Write-Host 'Extracting INTELLIVIZ and its bundled runtime...'
        & tar.exe -xf $archive -C $stage
        if ($LASTEXITCODE -ne 0) { throw "Extraction failed. Partial files are in $stage." }
        [IO.Directory]::Move($stage, $app)
    }
    $runtimeConfig = Get-ChildItem (Join-Path $app 'runtime') -Filter 'python*._pth' -File
    foreach ($config in $runtimeConfig) {
        if ((Get-Content -LiteralPath $config.FullName) -notcontains '..') {
            Add-Content -LiteralPath $config.FullName -Value '..' -Encoding Ascii
        }
    }
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $shortcut = $shell.CreateShortcut((Join-Path $desktop 'INTELLIVIZ.lnk'))
    $shortcut.TargetPath = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
    $launcher = Join-Path $app 'Start-INTELLIVIZ.ps1'
    $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -File "' + $launcher + '"'
    $shortcut.WorkingDirectory = $app
    $shortcut.Description = 'Open INTELLIVIZ'
    $icon = Join-Path $app 'assets\intelliviz.ico'
    $projectIcon = Join-Path (Split-Path -Parent $PSScriptRoot) 'assets\intelliviz.ico'
    if (Test-Path -LiteralPath $projectIcon -PathType Leaf) {
        Copy-Item -LiteralPath $projectIcon -Destination $icon -Force
    }
    if (Test-Path -LiteralPath $icon -PathType Leaf) {
        $shortcut.IconLocation = "$icon,0"
    }
    $shortcut.Save()
    Write-Host 'Installed. Open INTELLIVIZ from your desktop.'
} catch {
    Write-Host $_.Exception.Message
    exit 1
}
