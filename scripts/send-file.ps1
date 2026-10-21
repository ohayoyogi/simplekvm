# Transfer a local file to the Pi over the serial console (base64 encoded).
# The shell on the Pi must be at a prompt. Usage: .\send-file.ps1 local.tgz /tmp/remote.tgz [-Port COM6]
param(
    [Parameter(Mandatory, Position = 0)][string]$LocalPath,
    [Parameter(Mandatory, Position = 1)][string]$RemotePath,
    [string]$Port = "COM6",
    [int]$Baud = 115200
)

$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $LocalPath))
$b64 = [Convert]::ToBase64String($bytes)
$lines = for ($i = 0; $i -lt $b64.Length; $i += 76) { $b64.Substring($i, [Math]::Min(76, $b64.Length - $i)) }

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
try { $sp.Open() } catch { Write-Error "Cannot open ${Port}: $($_.Exception.InnerException.Message)"; exit 1 }
try {
    $sp.DiscardInBuffer()
    $sp.Write("stty -echo; base64 -d > '$RemotePath'; stty echo`r")
    Start-Sleep -Milliseconds 500
    $total = $lines.Count
    for ($n = 0; $n -lt $total; $n++) {
        $sp.Write($lines[$n] + "`n")
        if ($sp.BytesToRead -gt 0) { [void]$sp.ReadExisting() }
        if ($n % 2000 -eq 0) { Write-Host ("{0}/{1} lines" -f $n, $total) }
    }
    Start-Sleep -Milliseconds 500
    $sp.Write([string][char]4)   # Ctrl-D ends base64 input
    Start-Sleep -Seconds 2
    [void]$sp.ReadExisting()
    Write-Host "Sent $($bytes.Length) bytes. Local SHA256:"
    (Get-FileHash $LocalPath -Algorithm SHA256).Hash.ToLower()
} finally {
    $sp.Close()
}
