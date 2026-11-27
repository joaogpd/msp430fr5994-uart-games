<#
.SYNOPSIS
    Compile docs/ga/paper.tex into docs/ga/paper.pdf using MiKTeX / TeX Live pdflatex.
.EXAMPLE
    .\tools\build_paper.ps1
    .\tools\build_paper.ps1 -Open
    .\tools\build_paper.ps1 -KeepAux
#>
[CmdletBinding()]
param(
    [string]$TexFile = "",
    [switch]$Open,
    [switch]$KeepAux
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($TexFile)) {
    $TexFile = Join-Path $repoRoot "docs\ga\paper.tex"
}
if (-not (Test-Path $TexFile)) {
    Write-Error "TeX file not found: $TexFile"
    exit 1
}

function Find-PdfLatex {
    $cmd = Get-Command pdflatex -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe",
        "${env:ProgramFiles}\MiKTeX\miktex\bin\x64\pdflatex.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$pdflatex = Find-PdfLatex
if (-not $pdflatex) {
    Write-Error "pdflatex not found. Install MiKTeX or TeX Live and ensure pdflatex is on PATH."
    exit 1
}

$texDir  = Split-Path -Parent (Resolve-Path $TexFile)
$texName = Split-Path -Leaf $TexFile
$baseName = [System.IO.Path]::GetFileNameWithoutExtension($texName)
$pdfPath = Join-Path $texDir "$baseName.pdf"
$logPath = Join-Path $texDir "$baseName.log"

Push-Location $texDir
try {
    Write-Host "Compiling $texName with $pdflatex..." -ForegroundColor Cyan
    for ($pass = 1; $pass -le 2; $pass++) {
        Write-Host "  Pass $pass/2..." -ForegroundColor DarkGray
        & $pdflatex -interaction=nonstopmode -halt-on-error $texName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "pdflatex failed on pass $pass. Tail of ${baseName}.log:" -ForegroundColor Red
            if (Test-Path $logPath) {
                Get-Content $logPath -Tail 40
            }
            exit $LASTEXITCODE
        }
    }

    if (-not (Test-Path $pdfPath)) {
        Write-Error "Expected PDF not produced: $pdfPath"
        exit 1
    }

    $sizeKb = [Math]::Round((Get-Item $pdfPath).Length / 1KB, 1)
    Write-Host "SUCCESS: Built $pdfPath ($sizeKb KB)" -ForegroundColor Green

    if (Test-Path $logPath) {
        $warnings = Select-String -Path $logPath -Pattern "Overfull \\hbox|LaTeX Warning:|Undefined" | ForEach-Object { $_.Line }
        if ($warnings.Count -gt 0) {
            Write-Host "LaTeX Warnings ($($warnings.Count)):" -ForegroundColor Yellow
            $warnings | Select-Object -First 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
        }
    }

    if (-not $KeepAux) {
        foreach ($ext in @(".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".synctex.gz")) {
            $aux = Join-Path $texDir "$baseName$ext"
            if (Test-Path $aux) { Remove-Item $aux -Force -ErrorAction SilentlyContinue }
        }
    }

    if ($Open) {
        Start-Process -FilePath $pdfPath
    }
}
finally {
    Pop-Location
}
