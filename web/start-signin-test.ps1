# Starts a sign-in test copy at http://localhost:8001 with its own database
# (web\signin-test.sqlite3). Another copy on port 8000 is not touched.
#
# Reads from your Windows user environment, so no key is typed here:
#   RB_FIREBASE_API_KEY  the Firebase web API key
#   RB_INVITE_EMAILS     comma-separated emails allowed to sign in
#   ANTHROPIC_API_KEY    for the model calls
# Set each once with: setx NAME "value"   (then open a new window)
Set-Location (Split-Path $PSScriptRoot -Parent)

$missing = @()
foreach ($name in 'RB_FIREBASE_API_KEY', 'RB_INVITE_EMAILS', 'ANTHROPIC_API_KEY') {
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($value) { Set-Item "Env:$name" $value } else { $missing += $name }
}
if ($missing) {
    Write-Host "Not set yet: $($missing -join ', '). Use setx NAME `"value`", then run this again in a new window."
    Read-Host 'Press Enter to close'; exit 1
}

$env:RB_FIREBASE_PROJECT = 'plumbline-test-4d71a'  # the project ID, not its display name
$env:RB_DB = Join-Path (Get-Location) 'web\signin-test.sqlite3'
Remove-Item Env:RB_DEV -ErrorAction SilentlyContinue
Remove-Item Env:RB_INVITE_CODES -ErrorAction SilentlyContinue

# A fresh random secret each start signs the session cookie; restarting signs everyone out.
$bytes = New-Object byte[] 48
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:RB_SESSION_SECRET = [Convert]::ToBase64String($bytes)

Write-Host "Sign-in test copy: http://localhost:8001  (invited: $env:RB_INVITE_EMAILS)"
& .\.venv\Scripts\python.exe -m uvicorn web.app:app --port 8001
