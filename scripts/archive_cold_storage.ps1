param(
    [string]$ReportsRoot = "C:\Users\user1\OneDrive\Documents\Dejoia Trading Bot\kalshi-predictive-bot\reports",
    [string]$ArchiveRoot = "D:\Kalshi Bot Archive",
    [datetime]$Cutoff = [datetime]"2026-07-09T00:00:00"
)

$sourceRoot = [System.IO.Path]::GetFullPath($ReportsRoot).TrimEnd('\')
$archiveRootResolved = [System.IO.Path]::GetFullPath($ArchiveRoot).TrimEnd('\')
if (-not $sourceRoot.StartsWith("C:\Users\user1\OneDrive\Documents\Dejoia Trading Bot\kalshi-predictive-bot\reports", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Reports root escaped the intended workspace"
}
if (-not $archiveRootResolved.StartsWith("D:\Kalshi Bot Archive", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Archive root escaped D:\Kalshi Bot Archive"
}

$batch = Join-Path $archiveRootResolved ("cold-logs-before-" + $Cutoff.ToString("yyyyMMdd"))
New-Item -ItemType Directory -Force -Path $batch | Out-Null
$excluded = @("phase_nyc_", "phase_gh1", "phase_pmb", "phase_forecast_provenance")
$candidates = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File -Filter "*.log" |
    Where-Object {
        $candidate = $_
        $candidate.LastWriteTime -lt $Cutoff -and
        -not ($excluded | Where-Object { $candidate.FullName -like ("*\" + $_ + "*") })
    }

$manifest = @()
foreach ($file in $candidates) {
    $relative = $file.FullName.Substring($sourceRoot.Length).TrimStart('\')
    $destination = [System.IO.Path]::GetFullPath((Join-Path $batch $relative))
    if (-not $destination.StartsWith($batch, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Computed destination escaped archive batch: $destination"
    }
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    $destinationDirectory = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null
    Move-Item -LiteralPath $file.FullName -Destination $destination
    $movedHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
    if ($movedHash -ne $hash) {
        throw "Archive hash verification failed: $relative"
    }
    $manifest += [ordered]@{
        relative_path = $relative
        bytes = $file.Length
        last_write_time = $file.LastWriteTime.ToString("o")
        sha256 = $hash
        archived_path = $destination
    }
}

$manifestPath = Join-Path $batch "archive_manifest.json"
[ordered]@{
    generated_at = [datetime]::UtcNow.ToString("o")
    cutoff = $Cutoff.ToString("o")
    source_root = $sourceRoot
    archive_root = $batch
    files_moved = $manifest.Count
    bytes_moved = ($manifest | ForEach-Object { [long]$_['bytes'] } | Measure-Object -Sum).Sum
    current_rollback_backups_moved = 0
    active_runtime_reports_moved = 0
    files = $manifest
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Output $manifestPath
