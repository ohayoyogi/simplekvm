# Send a command over the serial console and print the response.
# Usage: .\serial.ps1 "command" [-Port COM6] [-Baud 115200] [-IdleMs 800] [-MaxMs 15000]
param(
    [Parameter(Position = 0)][string]$Command = "",
    [string]$Port = "COM6",
    [int]$Baud = 115200,
    [int]$IdleMs = 800,
    [int]$MaxMs = 15000
)

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
$sp.Encoding = [System.Text.Encoding]::UTF8
$sp.NewLine = "`r"
try { $sp.Open() } catch { Write-Error "Cannot open ${Port}: $($_.Exception.InnerException.Message)"; exit 1 }
try {
    $sp.DiscardInBuffer()
    $sp.Write($Command + "`r")

    $sb = New-Object System.Text.StringBuilder
    $start = [DateTime]::Now
    $last = [DateTime]::Now
    while ($true) {
        if ($sp.BytesToRead -gt 0) {
            [void]$sb.Append($sp.ReadExisting())
            $last = [DateTime]::Now
        } else {
            Start-Sleep -Milliseconds 50
        }
        $now = [DateTime]::Now
        if (($now - $last).TotalMilliseconds -gt $IdleMs) { break }
        if (($now - $start).TotalMilliseconds -gt $MaxMs) { break }
    }
    # Strip ANSI escape sequences for readability
    $sb.ToString() -replace "\x1b\[[0-9;?]*[A-Za-z]", ""
} finally {
    $sp.Close()
}
