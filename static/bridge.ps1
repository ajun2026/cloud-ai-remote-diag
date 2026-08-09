# ============================================================================
#  Cloud AI Remote Diagnostics - PowerShell Bridge (ps-pipe)
#  Version 1.0.2
#
#  A file-less PowerShell client that connects to the cloud server and
#  executes diagnostic commands on this Windows PC - no .exe needed.
#
#  Usage:
#    1) One-line, no download (interactive room prompt):
#         iex (iwr http://106.54.193.9:8000/static/bridge.ps1).Content
#
#    2) One-line with room code pre-set (no prompt):
#         $env:BRIDGE_ROOM = "ABC12345"; iex (iwr http://106.54.193.9:8000/static/bridge.ps1).Content
#
#    3) Download to local file, then run:
#         powershell -ExecutionPolicy Bypass -File bridge.ps1 -Room ABC12345
#         powershell -ExecutionPolicy Bypass -File bridge.ps1 -Server ws://10.0.0.5:8000 -Room ABC12345
#
#  Requirements: Windows PowerShell 5.1+ (built-in), .NET 4.5+
#  Note: keep the window open while diagnosing. The bridge auto-reconnects.
# ============================================================================

param(
    [string]$Server = $env:BRIDGE_SERVER,
    [string]$Room   = $env:BRIDGE_ROOM
)

$Version   = "1.0.2"
$ServerUrl = if ($Server) { $Server } else { "ws://106.54.193.9:8000" }
$script:ws = $null
$script:exiting = $false

# ---- banner ----
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Cloud AI Remote Diagnostics - PowerShell Bridge v$Version"
Write-Host "--------------------------------------------------"

if (-not $Room) {
    $Room = Read-Host " Room code (8 chars, e.g. ABC12345)"
}
$Room = $Room.Trim().ToUpper()
if (-not $Room) {
    Write-Host "Error: room code is required." -ForegroundColor Red
    exit 2
}

$ServerUrl = $ServerUrl.TrimEnd('/')
$wsUri = "$ServerUrl/ws/bridge/$Room"
Write-Host " Connecting to $wsUri"
Write-Host "==================================================" -ForegroundColor Cyan

# ---- audit log (local) ----
$script:logDir = Join-Path $env:TEMP "clouddiag-ps"
if (-not (Test-Path $script:logDir)) { New-Item -ItemType Directory -Path $script:logDir -Force | Out-Null }
$script:logFile = Join-Path $script:logDir "bridge.log"
function Write-Audit {
    param($Msg)
    try {
        Add-Content -Path $script:logFile -Value ((Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "  " + $Msg) -Encoding UTF8
    } catch {}
}

# ---- send lock: ClientWebSocket does not allow concurrent sends ----
# NOTE: must be $script: scoped! Background tasks (heartbeat / command runner)
# have no script scope chain - bare $sendLock resolves to $null there and
# .WaitOne() throws, silently killing the task (heartbeat stops, command
# results never sent). Same for $script:logFile above.
$script:sendLock = New-Object System.Threading.Mutex

function Send-Json {
    param($Obj)
    $script:sendLock.WaitOne() | Out-Null
    try {
        if ($script:ws -eq $null -or $script:ws.State -ne [System.Net.WebSockets.WebSocketState]::Open) { return }
        $json = $Obj | ConvertTo-Json -Compress -Depth 12
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $seg = New-Object 'System.ArraySegment[byte]' -ArgumentList (,[byte[]]$bytes)
        [void]($script:ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, [System.Threading.CancellationToken]::None).GetAwaiter().GetResult())
    } catch {
        Write-Host ("send error: " + $_.Exception.Message) -ForegroundColor Yellow
    } finally {
        $script:sendLock.ReleaseMutex()
    }
}

function Receive-Text {
    $buffer = New-Object byte[] 65536
    $ms = New-Object System.IO.MemoryStream
    while ($true) {
        $seg = New-Object 'System.ArraySegment[byte]' -ArgumentList (,[byte[]]$buffer)
        $res = $script:ws.ReceiveAsync($seg, [System.Threading.CancellationToken]::None).GetAwaiter().GetResult()
        if ($res.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
            return $null
        }
        $ms.Write($buffer, 0, $res.Count)
        if ($res.EndOfMessage) { break }
    }
    return [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
}

# ---- machine identity ----
function Get-LocalIP {
    try {
        $ips = [System.Net.Dns]::GetHostAddresses($env:COMPUTERNAME) |
            Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
            ForEach-Object { $_.IPAddressToString }
        if ($ips) { return ($ips | Select-Object -First 1) }
    } catch {}
    return ""
}

function Get-IsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = New-Object Security.Principal.WindowsPrincipal($id)
        return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Send-Identify {
    $info = [ordered]@{
        hostname = $env:COMPUTERNAME
        os       = "windows"
        platform = "windows"
        arch     = $env:PROCESSOR_ARCHITECTURE
        local_ip = (Get-LocalIP)
        username = $env:USERNAME
        version  = $Version
        bridge   = "ps-pipe"
        is_admin = (Get-IsAdmin)
    }
    Send-Json @{ type = "identify"; info = $info }
    Write-Audit "identify sent (host=$($env:COMPUTERNAME), ip=$(Get-LocalIP), is_admin=$(Get-IsAdmin))"
}

# ---- command execution ----
function Execute-CommandSpec {
    param($Spec)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $timeoutSec = if ($Spec.timeout -and $Spec.timeout -gt 0) { [int]$Spec.timeout } else { 60 }
    $timeoutMs  = $timeoutSec * 1000
    $cmdStr = [string]$Spec.command
    $shell  = [string]$Spec.shell
    if (-not $shell) { $shell = "auto" }

    Write-Audit "cmd [$shell] timeout=${timeoutSec}s: $($cmdStr.Substring(0, [Math]::Min(160, $cmdStr.Length)))"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8
    if ($Spec.cwd -and (Test-Path $Spec.cwd)) { $psi.WorkingDirectory = [string]$Spec.cwd }

    switch ($shell) {
        "cmd" {
            $psi.FileName = "cmd.exe"
            $psi.Arguments = "/c `"$cmdStr`""
        }
        "bash" {
            $psi.FileName = "bash.exe"
            $psi.Arguments = "-c `"$cmdStr`""
        }
        default {
            # powershell / pwsh / auto - use -EncodedCommand to avoid
            # quote escaping and encoding traps entirely.
            $psi.FileName = "powershell.exe"
            $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmdStr))
            $psi.Arguments = "-NoProfile -NonInteractive -EncodedCommand $enc"
        }
    }

    $output = ""
    $err    = ""
    $exitCode = -1
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($timeoutMs)) {
            try {
                Start-Process -FilePath "taskkill.exe" -ArgumentList "/F","/T","/PID",$p.Id -NoNewWindow -Wait | Out-Null
            } catch {}
            $p.WaitForExit(5000) | Out-Null
            $err = "command timed out after ${timeoutSec}s"
        }
        $output = $outTask.GetAwaiter().GetResult()
        $errText = $errTask.GetAwaiter().GetResult()
        if ($errText) { $err = ($err + $errText).Trim() }
        $exitCode = $p.ExitCode
    } catch {
        $err = $_.Exception.Message
    }
    $sw.Stop()

    Send-Json @{
        type        = "command_result"
        id          = [string]$Spec.id
        output      = $output
        exit_code   = $exitCode
        error       = $err
        duration_ms = $sw.ElapsedMilliseconds
    }
    Write-Audit "result id=$($Spec.id) exit=$exitCode duration=$($sw.ElapsedMilliseconds)ms outlen=$($output.Length)"
}

function Execute-CommandSpecFromJson {
    param($JsonText)
    try {
        $spec = $JsonText | ConvertFrom-Json
        Execute-CommandSpec $spec
    } catch {
        Write-Host ("command task error: " + $_.Exception.Message) -ForegroundColor Yellow
        Write-Audit ("command task error: " + $_.Exception.ToString())
    }
}

# ---- file channel ----
function Handle-FileDownload {
    param($Msg)
    $path = [string]$Msg.path
    try {
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $name  = [System.IO.Path]::GetFileName($path)
        $chunkSize = 262144  # 256KB per chunk (matches Go bridge)
        $total = [Math]::Max(1, [Math]::Ceiling($bytes.Length / $chunkSize))
        for ($i = 0; $i -lt $total; $i++) {
            $offset = $i * $chunkSize
            $len = [Math]::Min($chunkSize, $bytes.Length - $offset)
            $chunk = New-Object byte[] $len
            [Array]::Copy($bytes, $offset, $chunk, 0, $len)
            Send-Json @{
                type  = "file_download_result"
                id    = [string]$Msg.id
                path  = $path
                name  = $name
                data  = [Convert]::ToBase64String($chunk)
                chunk = $i
                total = $total
                size  = $bytes.Length
            }
        }
        Write-Audit "file_download OK $path ($($bytes.Length) bytes, $total chunks)"
        Write-Host ("file uploaded: $path ($($bytes.Length) bytes)") -ForegroundColor DarkGray
    } catch {
        Send-Json @{ type = "file_download_error"; id = [string]$Msg.id; error = $_.Exception.Message }
        Write-Audit "file_download ERROR $path : $($_.Exception.Message)"
    }
}

function Handle-FileUpload {
    param($Msg)
    $dir = Join-Path $env:TEMP "clouddiag"
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $name = [System.IO.Path]::GetFileName([string]$Msg.name)
    if (-not $name) { $name = [System.IO.Path]::GetFileName([string]$Msg.path) }
    $target = Join-Path $dir $name
    try {
        $data = [Convert]::FromBase64String([string]$Msg.data)
        $mode = if ($Msg.chunk -eq 0) { [System.IO.FileMode]::Create } else { [System.IO.FileMode]::Append }
        $fs = New-Object System.IO.FileStream($target, $mode, [System.IO.FileAccess]::Write)
        try { $fs.Write($data, 0, $data.Length) } finally { $fs.Close() }
        if (($Msg.chunk + 1) -ge $Msg.total) {
            Send-Json @{ type = "file_upload_result"; id = [string]$Msg.id; path = $target }
            Write-Audit "file_upload OK $target"
            Write-Host ("file received: $target") -ForegroundColor DarkGray
        }
    } catch {
        Send-Json @{ type = "file_upload_result"; id = [string]$Msg.id; path = "" }
        Write-Audit "file_upload ERROR $target : $($_.Exception.Message)"
    }
}

# ---- main connection loop (one connect + read loop) ----
# v2 fix: everything runs on the main thread (no background tasks).
# Why: PowerShell Task.Run/StartNew scriptblocks have no script scope chain,
# accessing $script: vars silently fails -> heartbeat/command results never sent
# (KJTFF8HA/A7P7J38W reproduced). Sync mode is safe: ClientWebSocket has no
# read deadline (server never kicks us for a heartbeat gap); messages queue
# in the TCP buffer while a command runs and are processed afterwards.
#
# Keepalive design (v1.0.2): NEVER cancel ReceiveAsync to send heartbeats -
# cancelling kills the connection (reproduced: disconnect code=1005 exactly
# 25s after connect, every cycle). Instead:
#   - ClientWebSocketOptions.KeepAliveInterval sends protocol-level WebSocket
#     ping frames internally (.NET handles them, no app code, no main thread)
#   - a JSON heartbeat is piggy-backed whenever a message arrives and 25s
#     have passed, so the server's heartbeat_age stays fresh during diagnosis
function Run-Bridge {
    param($Uri)
    $script:ws = New-Object System.Net.WebSockets.ClientWebSocket
    $script:ws.Options.KeepAliveInterval = [TimeSpan]::FromSeconds(25)
    [void]($script:ws.ConnectAsync($Uri, [System.Threading.CancellationToken]::None).GetAwaiter().GetResult())
    Write-Host "Connected. Waiting for server..." -ForegroundColor Green
    Write-Audit "connected to $Uri"
    Send-Identify

    $epoch = [DateTime]::new(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
    $lastHb = Get-Date

    while (-not $script:exiting -and $script:ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
        $text = Receive-Text
        if ($null -eq $text) { break }
        if (-not $text) { continue }

        # opportunistic heartbeat: piggy-back on any server message
        if (([DateTime]::UtcNow - $lastHb.ToUniversalTime()).TotalSeconds -ge 25) {
            $ts = [int64]([DateTime]::UtcNow - $epoch).TotalSeconds
            Send-Json @{ type = "heartbeat"; ts = $ts }
            $lastHb = Get-Date
        }

        $msg = $null
        try { $msg = $text | ConvertFrom-Json } catch { continue }

        switch ($msg.type) {
            "identify_request" { Send-Identify }
            "ping"             { Send-Json @{ type = "pong" } }
            "pong"             { }  # server's reply to heartbeat, ignore
            "status"           { Write-Host ("[server] " + $msg.content) -ForegroundColor DarkCyan }
            "command"          { Execute-CommandSpecFromJson $text }  # synchronous (agent sends one at a time)
            "file_download"    { Handle-FileDownload $msg }
            "file_upload"      { Handle-FileUpload $msg }
            "close" {
                Write-Host ("closed by server: " + $msg.reason) -ForegroundColor Yellow
                $script:exiting = $true
                break
            }
            default            { Write-Host ("unknown msg type: " + $msg.type) -ForegroundColor DarkGray }
        }
    }

    try {
        if ($script:ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
            [void]($script:ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "bye", [System.Threading.CancellationToken]::None).GetAwaiter().GetResult())
        }
    } catch {}
    $script:ws.Dispose()
    $script:ws = $null
    Write-Audit "disconnected"
}

# ---- auto-reconnect loop (3s -> 30s backoff) ----
$attempt = 0
$delay = 3
while (-not $script:exiting) {
    $attempt++
    try {
        Run-Bridge ([Uri]$wsUri)
        Write-Host "Connection closed." -ForegroundColor DarkGray
    } catch {
        Write-Host ("Connection error: " + $_.Exception.Message) -ForegroundColor Yellow
        Write-Audit "connection error: $($_.Exception.Message)"
    }
    if ($script:exiting) { break }
    Write-Host ("Reconnecting in ${delay}s (attempt $attempt)...") -ForegroundColor DarkGray
    Start-Sleep -Seconds $delay
    if ($delay -lt 30) { $delay = $delay * 2 }
}
Write-Host "Bridge exited."
