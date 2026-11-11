# Offline Windows file preparation only; no service is installed or started.
# Requires elevation. Caddy and nps normally run on the Linux VPS.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$VerifiedServerExtractDir,
    [Parameter(Mandatory=$true)][string]$Domain,
    [string]$InstallRoot = "$env:ProgramFiles\nps"
)
$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run from an elevated PowerShell."
}
if (Test-Path -LiteralPath $InstallRoot) { throw "Refusing to overwrite existing $InstallRoot." }
foreach ($relative in @('nps.exe', 'web\views', 'web\static')) {
    if (-not (Test-Path -LiteralPath (Join-Path $VerifiedServerExtractDir $relative))) { throw "Missing verified nps file: $relative" }
}
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Path (Join-Path $InstallRoot 'conf') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $VerifiedServerExtractDir 'nps.exe') -Destination (Join-Path $InstallRoot 'nps.exe')
Copy-Item -LiteralPath (Join-Path $VerifiedServerExtractDir 'web') -Destination (Join-Path $InstallRoot 'web') -Recurse
Copy-Item -LiteralPath (Join-Path $root 'scripts\nps.conf.example') -Destination (Join-Path $InstallRoot 'conf\nps.conf')
foreach ($name in @('clients.json', 'hosts.json', 'tasks.json')) {
    [IO.File]::WriteAllText((Join-Path $InstallRoot "conf\$name"), '{}', [Text.UTF8Encoding]::new($false))
}
Write-Host "1. Edit $InstallRoot\conf\nps.conf: set VPS_IP and random admin credentials."
Write-Host "2. Ensure nps.exe is stopped, then run:"
Write-Host "   python $root\qoder_public_tunnel.py prepare-server --nps-root '$InstallRoot' --domain $Domain --vkey-output '$InstallRoot\npc-vkey.txt' --yes-stopped"
Write-Host "3. Restrict secrets with icacls, then register/start nps.exe explicitly with your chosen Windows service manager."
Write-Host "4. Linux VPS systemd operations are documented in tunnel/qoder-nps.service.example."
