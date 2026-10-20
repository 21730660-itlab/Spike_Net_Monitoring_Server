# Spike Net remote-access helper for technician PCs (Windows PowerShell 5.1, no extra software).
#   Install:   right-click install.bat -> Run (no administrator rights needed)
#   Uninstall: run this script with -Uninstall
# The browser opens spikenet://open?server=...&token=... ; this script asks the Spike Net server for
# the login behind that one-time token (valid 60 seconds) and starts Winbox or an SSH client.
param([string]$Url = "", [switch]$Install, [switch]$Uninstall)

$ErrorActionPreference = "Stop"
$AppDir = Join-Path $env:LOCALAPPDATA "SpikeNet"
$ConfigFile = Join-Path $AppDir "config.json"
$Key = "HKCU:\Software\Classes\spikenet"

function Show-Message([string]$text, [string]$icon = "Information") {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($text, "Spike Net helper", "OK", $icon) | Out-Null
}

function Find-Winbox {
    $candidates = @(
        "$env:ProgramFiles\MikroTik\WinBox\WinBox.exe", "$env:ProgramFiles\WinBox\WinBox.exe",
        "$env:ProgramFiles\Winbox\winbox64.exe", "${env:ProgramFiles(x86)}\Winbox\winbox.exe",
        "$AppDir\winbox64.exe", "$AppDir\winbox.exe", "$AppDir\WinBox.exe"
    )
    foreach ($dir in @("$env:USERPROFILE\Desktop", "$env:USERPROFILE\Downloads", $PSScriptRoot)) {
        if ($dir -and (Test-Path $dir)) {
            $candidates += Get-ChildItem -Path $dir -Filter "winbox*.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
        }
    }
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Where is winbox.exe on this PC?"
    $dialog.Filter = "Winbox|winbox*.exe;WinBox.exe|All programs|*.exe"
    if ($dialog.ShowDialog() -eq "OK") { return $dialog.FileName }
    return ""
}

if ($Uninstall) {
    Remove-Item -Path $Key -Recurse -ErrorAction SilentlyContinue
    Show-Message "The Spike Net helper was removed."
    exit 0
}

if ($Install) {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Copy-Item -Path $PSCommandPath -Destination (Join-Path $AppDir "spikenet-helper.ps1") -Force
    $config = @{}
    $shipped = Join-Path $PSScriptRoot "config.json"
    if (Test-Path $shipped) { $config = Get-Content $shipped -Raw | ConvertFrom-Json }
    $winbox = Find-Winbox
    $out = [ordered]@{ server = $config.server; winbox = $winbox }
    $out | ConvertTo-Json | Set-Content -Path $ConfigFile -Encoding UTF8
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$AppDir\spikenet-helper.ps1`" `"%1`""
    New-Item -Path $Key -Force | Out-Null
    Set-ItemProperty -Path $Key -Name "(default)" -Value "URL:Spike Net helper"
    New-ItemProperty -Path $Key -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
    New-Item -Path "$Key\shell\open\command" -Force | Out-Null
    Set-ItemProperty -Path "$Key\shell\open\command" -Name "(default)" -Value $command
    $wb = if ($winbox) { $winbox } else { "not found (Winbox buttons will ask again)" }
    Show-Message "Installed.`n`nServer: $($config.server)`nWinbox: $wb`n`nThe Winbox and SSH buttons in Spike Net now open on this PC."
    exit 0
}

try {
    $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
    $uri = [Uri]$Url
    $query = @{}
    foreach ($pair in $uri.Query.TrimStart("?").Split("&")) {
        $kv = $pair.Split("=", 2)
        if ($kv.Count -eq 2) { $query[$kv[0]] = [Uri]::UnescapeDataString($kv[1]) }
    }
    $server = $query["server"].TrimEnd("/")
    if ($cfg.server -and ($server -ne $cfg.server.TrimEnd("/"))) {
        Show-Message "This link comes from $server, but the helper is set up for $($cfg.server). Nothing was opened." "Warning"
        exit 1
    }
    $info = Invoke-RestMethod -Uri "$server/api/launch/$($query['token'])" -Method Get -TimeoutSec 10
    $address = "$($info.host):$($info.port)"
    if ($info.kind -eq "winbox") {
        $winbox = $cfg.winbox
        if (-not $winbox -or -not (Test-Path $winbox)) {
            $winbox = Find-Winbox
            if (-not $winbox) { exit 1 }
            $cfg.winbox = $winbox
            $cfg | ConvertTo-Json | Set-Content -Path $ConfigFile -Encoding UTF8
        }
        $q = { param($s) '"' + ($s -replace '"', '\"') + '"' }
        Start-Process -FilePath $winbox -ArgumentList ((& $q $address) + " " + (& $q $info.username) + " " + (& $q $info.password))
    } elseif ($info.kind -eq "ssh") {
        $putty = @("$env:ProgramFiles\PuTTY\putty.exe", "${env:ProgramFiles(x86)}\PuTTY\putty.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        if ($putty) {
            Start-Process -FilePath $putty -ArgumentList @("-ssh", "$($info.username)@$($info.host)", "-P", "$($info.port)", "-pw", $info.password)
        } else {
            if ($info.password) { Set-Clipboard -Value $info.password }
            Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "echo Password copied to the clipboard - right-click to paste it. & ssh -p $($info.port) $($info.username)@$($info.host)"
        }
    }
} catch {
    Show-Message "Could not open the device: $($_.Exception.Message)" "Error"
    exit 1
}
