param()

$ErrorActionPreference = "SilentlyContinue"

try {
    $raw = [Console]::In.ReadToEnd()
    $payload = if ($raw) { $raw | ConvertFrom-Json } else { $null }

    # Never loop if a Stop hook continuation already ran, subagents are still active,
    # or the turn ended due to error/cancel/limit rather than normal completion.
    if ($payload) {
        if ($payload.continuationActive -eq $true -or
            $payload.fullyIdle -eq $false -or
            ($null -ne $payload.executionNum -and [int]$payload.executionNum -gt 0)) {
            Write-Output '{}'
            exit 0
        }
        if ($payload.terminationReason -and $payload.terminationReason -ne 'NO_TOOL_CALL') {
            Write-Output '{}'
            exit 0
        }

        # Scope check strictly to conversations that actually edited a game file.
        # Prevents read-only subagents (e.g., code-reviewer, game-designer) from ever
        # being blocked by uncommitted changes made by the parent agent.
        if ($payload.transcriptPath -and (Test-Path $payload.transcriptPath)) {
            $editPattern = '"name"\s*:\s*"(edit_file|write_file)"'
            $gameFilePattern = '(app[\\/]+[^"]+\.(c|h)|main\.c|play\.bat|launch_terminal\.ps1)'
            $touchedGameFile = $false

            foreach ($tline in (Get-Content -Path $payload.transcriptPath -ErrorAction SilentlyContinue)) {
                if (($tline -match $editPattern) -and ($tline -match $gameFilePattern)) {
                    $touchedGameFile = $true
                    break
                }
            }

            if (-not $touchedGameFile) {
                Write-Output '{}'
                exit 0
            }
        }
    }

    # Script lives in <repo>/.agents/scripts/check_manual_sync.ps1
    $agentsDir  = Split-Path -Parent $PSScriptRoot
    $repoRoot   = Split-Path -Parent $agentsDir
    $manualPath = Join-Path $repoRoot "docs\HOW_TO_PLAY.md"

    $statusLines = & git -C $repoRoot status --porcelain 2>$null
    $gameRegex = '^(app/.*\.(c|h)|main\.c|play\.bat|tools/launch_terminal\.ps1)$'
    $changedGameFiles = @()

    foreach ($line in $statusLines) {
        if ($line.Length -lt 4) { continue }
        $relPath = $line.Substring(3).Trim().Trim('"')
        if ($relPath -match $gameRegex) {
            $fullPath = Join-Path $repoRoot ($relPath -replace '/', '\')
            if (Test-Path $fullPath) {
                $changedGameFiles += [PSCustomObject]@{
                    RelPath = $relPath
                    MTime   = (Get-Item $fullPath).LastWriteTimeUtc
                }
            }
        }
    }

    if (-not (Test-Path $manualPath)) {
        $resp = @{
            decision = "continue"
            reason   = "docs/HOW_TO_PLAY.md does not exist yet. Follow .agents/skills/game-manual-maintainer/SKILL.md to generate the player manual before finishing."
        } | ConvertTo-Json -Compress
        Write-Output $resp
        exit 0
    }

    if ($changedGameFiles.Count -eq 0) {
        Write-Output '{}'
        exit 0
    }

    $manualMTime = (Get-Item $manualPath).LastWriteTimeUtc
    $newerFiles = $changedGameFiles | Where-Object { $_.MTime -gt $manualMTime }

    if ($newerFiles.Count -gt 0) {
        $names = ($newerFiles | ForEach-Object { $_.RelPath }) -join ", "
        $resp = @{
            decision = "continue"
            reason   = "Game files ($names) were modified more recently than docs/HOW_TO_PLAY.md. Run the game-manual-maintainer subagent (following .agents/skills/game-manual-maintainer/SKILL.md) to update docs/HOW_TO_PLAY.md before finishing."
        } | ConvertTo-Json -Compress
        Write-Output $resp
        exit 0
    }

    Write-Output '{}'
    exit 0
}
catch {
    Write-Output '{}'
    exit 0
}
