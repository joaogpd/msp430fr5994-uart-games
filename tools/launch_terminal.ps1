<#
.SYNOPSIS
    Instantly launch an ANSI/VT100 UART terminal connected to the MSP430FR5994 LaunchPad.
.DESCRIPTION
    Auto-detects the "MSP Application UART1" COM port via WMI/PnP and opens either
    PuTTY (dedicated VT100 window) or an inline raw-mode ANSI terminal using .NET SerialPort.
.EXAMPLE
    .\tools\launch_terminal.ps1
    .\tools\launch_terminal.ps1 -Baud 115200
    .\tools\launch_terminal.ps1 -Mode inline -Baud 9600
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet(9600, 19200, 38400, 57600, 115200)]
    [int]$Baud = 115200,

    [Parameter(Position = 1)]
    [ValidateSet("auto", "putty", "inline")]
    [string]$Mode = "auto",

    [string]$Port = ""
)

$ErrorActionPreference = "Stop"

function Find-MspUartPort {
    $devices = Get-CimInstance Win32_PnPEntity -Filter "Name LIKE '%(COM%'" -ErrorAction SilentlyContinue
    if (-not $devices) { return $null }

    # Primary match: TI eZ-FET Backchannel UART friendly name
    $match = $devices | Where-Object { $_.Name -match 'MSP Application UART1' } | Select-Object -First 1
    if (-not $match) {
        # Fallback match: TI LaunchPad composite USB VID_2047&PID_0013 interface 02
        $match = $devices | Where-Object { $_.DeviceID -match 'VID_2047&PID_0013&MI_02' } | Select-Object -First 1
    }

    if ($match -and ($match.Name -match '\((COM\d+)\)')) {
        return $Matches[1]
    }
    return $null
}

function Find-PuttyExe {
    $cmd = Get-Command putty -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "${env:ProgramFiles}\PuTTY\putty.exe",
        "${env:ProgramFiles(x86)}\PuTTY\putty.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Enable-ConsoleAnsi {
    $sig = @"
using System;
using System.Runtime.InteropServices;
public static class Win32Console {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
}
"@
    Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue | Out-Null
    $handle = [Win32Console]::GetStdHandle(-11) # STD_OUTPUT_HANDLE
    [uint32]$mode = 0
    if ([Win32Console]::GetConsoleMode($handle, [ref]$mode)) {
        [void][Win32Console]::SetConsoleMode($handle, ($mode -bor 0x0004)) # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    }
}

function Start-InlineTerminal([string]$ComPort, [int]$BaudRate) {
    Enable-ConsoleAnsi

    $serial = New-Object System.IO.Ports.SerialPort($ComPort, $BaudRate, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $serial.Handshake = [System.IO.Ports.Handshake]::None
    $serial.ReadTimeout = 20
    $serial.WriteTimeout = 500
    $serial.Encoding = [System.Text.Encoding]::ASCII

    $prevTreatCtrlC = [Console]::TreatControlCAsInput
    [Console]::TreatControlCAsInput = $true

    try {
        $serial.Open()
        # Request an immediate full-screen redraw (Ctrl+L) so no RESET press is needed
        $serial.Write([byte[]]@(0x0C), 0, 1)
        Write-Host "Connected to $ComPort @ ${BaudRate} 8N1 (Ctrl+] to exit; Ctrl+L to redraw)" -ForegroundColor DarkGray

        $buf = New-Object byte[] 1024
        $utf8 = [System.Text.Encoding]::ASCII

        while ($true) {
            while ($serial.BytesToRead -gt 0) {
                $count = $serial.Read($buf, 0, [Math]::Min($buf.Length, $serial.BytesToRead))
                if ($count -gt 0) {
                    [Console]::Write($utf8.GetString($buf, 0, $count))
                }
            }

            while ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                # Ctrl+] (0x1D) exits cleanly
                if ([int]$key.KeyChar -eq 29) {
                    return
                }

                if ($key.KeyChar -ne [char]0) {
                    $b = [byte][int]$key.KeyChar
                    $serial.Write([byte[]]@($b), 0, 1)
                }
                else {
                    $esc = switch ($key.Key) {
                        'UpArrow'    { "$([char]27)[A" }
                        'DownArrow'  { "$([char]27)[B" }
                        'RightArrow' { "$([char]27)[C" }
                        'LeftArrow'  { "$([char]27)[D" }
                        default      { $null }
                    }
                    if ($esc) {
                        $bytes = $utf8.GetBytes($esc)
                        $serial.Write($bytes, 0, $bytes.Length)
                    }
                }
            }

            Start-Sleep -Milliseconds 5
        }
    }
    finally {
        [Console]::TreatControlCAsInput = $prevTreatCtrlC
        if ($serial.IsOpen) { $serial.Close() }
        $serial.Dispose()
        [Console]::Write("$([char]27)[?25h$([char]27)[0m`r`n")
    }
}

if ([string]::IsNullOrWhiteSpace($Port)) {
    $Port = Find-MspUartPort
}

if ([string]::IsNullOrWhiteSpace($Port)) {
    Write-Error "Could not find 'MSP Application UART1'. Check USB connection and eZ-FET jumpers, or pass -Port COMx."
    exit 1
}

$puttyPath = Find-PuttyExe
if ($Mode -eq "auto") {
    $Mode = if ($puttyPath) { "putty" } else { "inline" }
}

if ($Mode -eq "putty") {
    if (-not $puttyPath) {
        Write-Error "PuTTY was not found. Re-run with -Mode inline to use the built-in terminal."
        exit 1
    }
    Write-Host "Launching PuTTY on $Port @ $Baud 8N1..." -ForegroundColor Green
    Start-Process -FilePath $puttyPath -ArgumentList "-serial", $Port, "-sercfg", "$Baud,8,n,1,N"
}
else {
    Start-InlineTerminal -ComPort $Port -BaudRate $Baud
}
