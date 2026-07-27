# Opens Car Harness in its own window -- no address bar, no tabs, its own
# taskbar icon. That is Chrome's "--app" mode; opening the URL normally gives
# you a browser tab instead, which is the difference you can see on the taskbar.
#
# Called by start.bat once the server is up, and by the desktop shortcut.
# Falls back to the default browser if neither Chrome nor Edge is installed.

param(
    [string]$Url = 'http://127.0.0.1:8765',
    [int]$WaitSeconds = 30
)

# Wait for the server to actually answer before opening anything -- opening
# early just shows the user a connection error.
$deadline = (Get-Date).AddSeconds($WaitSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $null = Invoke-WebRequest "$Url/api/ping" -UseBasicParsing -TimeoutSec 1
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 400
    }
}

if (-not $ready) {
    Write-Host "The server at $Url did not answer in $WaitSeconds seconds."
    Write-Host "Leave this window open and try the shortcut again in a moment."
    exit 1
}

$candidates = @(
    (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
)

$browser = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($browser) {
    Start-Process -FilePath $browser -ArgumentList "--app=$Url", '--window-size=1500,1000'
} else {
    # No Chromium-based browser: a normal tab is better than nothing.
    Start-Process $Url
}
