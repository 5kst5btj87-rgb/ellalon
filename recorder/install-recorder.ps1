<#
  Ellalon recording server - installer for Windows 10 / 11 and Windows Server (64-bit)

  Run in PowerShell **as Administrator**:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder/install-recorder.ps1))) -Server http://ELLALON-SERVER -Code XXXX-XXXX

  The code comes from the Ellalon web UI: Storage -> + Add storage -> Recording server.
  Installs a private Python and FFmpeg under "C:\Program Files\Ellalon Recorder", pairs with the
  server and registers the "Ellalon Recorder" startup task (runs as SYSTEM at boot, restarts on
  failure). It records from its saved camera list even when the Ellalon server is down.
#>
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [Parameter(Mandatory = $true)][string]$Code,
  [int]$Port = 8090,
  [string]$Source = "https://raw.githubusercontent.com/5kst5btj87-rgb/ellalon/main/recorder"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Step($t) { Write-Host "==> $t" -ForegroundColor Green }

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Please run PowerShell as Administrator (right-click PowerShell -> Run as administrator)."
}
if (-not [Environment]::Is64BitOperatingSystem) { throw "64-bit Windows is required." }

$Dir = Join-Path $env:ProgramFiles "Ellalon Recorder"
$Task = "Ellalon Recorder"
$PyVer = "3.12.7"
$Tmp = Join-Path $env:TEMP "ellalon-recorder-setup"
New-Item -ItemType Directory -Force -Path $Dir, $Tmp | Out-Null

# stop a running copy before replacing files
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
  Get-Process python, ffmpeg -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Dir) } | Stop-Process -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path "$Dir\python\python.exe")) {
  Step "Downloading Python $PyVer (private copy)"
  Invoke-WebRequest "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile "$Tmp\python.zip"
  Expand-Archive "$Tmp\python.zip" "$Dir\python" -Force
}
if (-not (Test-Path "$Dir\ffmpeg.exe")) {
  Step "Downloading FFmpeg"
  Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile "$Tmp\ffmpeg.zip"
  Expand-Archive "$Tmp\ffmpeg.zip" "$Tmp\ffmpeg" -Force
  $exe = Get-ChildItem "$Tmp\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
  Copy-Item $exe.FullName "$Dir\ffmpeg.exe" -Force
}

Step "Downloading the Ellalon recorder"
Invoke-WebRequest "$Source/ellalon_recorder.py" -OutFile "$Dir\ellalon_recorder.py"

Step "Pairing with $Server"
& "$Dir\python\python.exe" "$Dir\ellalon_recorder.py" register --server $Server --code $Code --port $Port
if ($LASTEXITCODE -ne 0) { throw "Pairing failed - see the message above." }

Step "Installing the startup task (runs at boot, restarts on failure)"
$action = New-ScheduledTaskAction -Execute "$Dir\python\python.exe" -Argument "`"$Dir\ellalon_recorder.py`" run" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $Task

Step "Opening TCP $Port in Windows Firewall (playback)"
if (-not (Get-NetFirewallRule -DisplayName $Task -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $Task -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}
Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Ellalon recorder is installed and running." -ForegroundColor Green
Write-Host "  It starts automatically with Windows and keeps recording even when the Ellalon server is down."
Write-Host "  Cameras: Ellalon web UI -> Cameras -> Edit -> Recording storage -> this recording server."
Write-Host "  Status:  & `"$Dir\python\python.exe`" `"$Dir\ellalon_recorder.py`" status"
Write-Host "  Log:     $env:ProgramData\EllalonRecorder\recorder.log"
Write-Host "  Folder:  C:\EllalonRecordings (unless changed in the Storage tab)"
