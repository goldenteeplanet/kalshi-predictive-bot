$ErrorActionPreference = 'Stop'

# Keep the Ubuntu WSL instance alive so its enabled user services continue to
# run when no interactive terminal or Codex command is attached.
$existing = & "$env:SystemRoot\System32\wsl.exe" -d Ubuntu -- bash -lc `
    "pgrep -f '^kalshi-scheduler-keepalive infinity$' || true"
if ($existing) {
    exit 0
}
& "$env:SystemRoot\System32\wsl.exe" -d Ubuntu -- bash -lc `
    'exec -a kalshi-scheduler-keepalive sleep infinity'
