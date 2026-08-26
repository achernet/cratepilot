$ErrorActionPreference = "Stop"
$stateDirectory = Join-Path $env:LOCALAPPDATA "CratePilot"
$libraryFile = Join-Path $stateDirectory "library-root.txt"
$library = $null

if (Test-Path $libraryFile) {
    $library = (Get-Content $libraryFile -Raw).Trim()
    if (-not (Test-Path -LiteralPath $library -PathType Container)) {
        $library = $null
    }
}

if (-not $library) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = "Choose the music folder CratePilot may read"
    $dialog.ShowNewFolderButton = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        exit 0
    }
    $library = $dialog.SelectedPath
    New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
    Set-Content -Path $libraryFile -Value $library -Encoding UTF8
}

& (Join-Path $PSScriptRoot "bin\cratepilot.cmd") --library $library
exit $LASTEXITCODE
