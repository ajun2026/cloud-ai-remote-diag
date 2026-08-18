# dump_analyze.ps1 - BSOD / Dump Analysis (Windows)
# Layer 1: Event log signals (Kernel-Power/WHEA/Memory/Storage/Thermal)
# Layer 2: Minidump header parse (Bugcheck code + params + time)
# Layer 3: WinDbg deep analysis (optional)
# Output: structured ASCII (server side matches knowledge base)

$ErrorActionPreference = 'SilentlyContinue'
$since = (Get-Date).AddDays(-30)

# ===== Layer 1: Event Logs =====
$kp    = (Get-WinEvent -FilterHashtable @{LogName='System'; Id=41;   StartTime=$since}).Count
$whea  = (Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$since}).Count
$mem   = (Get-WinEvent -FilterHashtable @{LogName='System'; Id=1001; StartTime=$since}).Count
$disk  = (Get-WinEvent -FilterHashtable @{LogName='System'; Id=7,51,55; StartTime=$since}).Count
$therm = (Get-WinEvent -FilterHashtable @{LogName='System'; Id=86;   StartTime=$since}).Count
$bugchkEv = Get-WinEvent -FilterHashtable @{LogName='System'; Id=1001; StartTime=$since} -MaxEvents 1

"[EVENTS]"
"KERNEL_POWER_41=$kp"
"WHEA=$whea"
"MEMORY_DIAG=$mem"
"DISK_ERR=$disk"
"THERMAL=$therm"
if ($bugchkEv) {
    $m = [regex]::Match($bugchkEv.Message, '0x[0-9a-fA-F]{8}')
    "BUGCHECK_MSG=$($m.Value)"
    "BUGCHECK_TIME=$($bugchkEv.TimeCreated)"
}

# ===== Layer 2: Minidump parse =====
"[DUMPS]"
$dumps = @(Get-ChildItem 'C:\Windows\Minidump\*.dmp' | Sort-Object LastWriteTime -Descending | Select-Object -First 5)
if (Test-Path 'C:\Windows\MEMORY.DMP') { $dumps += Get-Item 'C:\Windows\MEMORY.DMP' }

if ($dumps.Count -eq 0) {
    "[NO_DUMP]"
} else {
    foreach ($d in $dumps) {
        $fs = [System.IO.File]::OpenRead($d.FullName)
        try {
            $buf = New-Object byte[] 0x60
            $fs.Read($buf, 0, 0x60) | Out-Null
        } finally { $fs.Close() }
        $sig = [System.Text.Encoding]::ASCII.GetString($buf, 0, 4)
        if ($sig -match 'PAGE|DU64|DUMP') {
            $code = [BitConverter]::ToUInt32($buf, 0x38)
            $p1 = [BitConverter]::ToUInt64($buf, 0x40)
            $p2 = [BitConverter]::ToUInt64($buf, 0x48)
            $p3 = [BitConverter]::ToUInt64($buf, 0x50)
            $p4 = [BitConverter]::ToUInt64($buf, 0x58)
            "FILE=$($d.Name)"
            "TIME=$($d.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
            "BUGCHECK=0x{0:X8}" -f $code
            "PARAMS=0x{0:X}|0x{1:X}|0x{2:X}|0x{3:X}" -f $p1, $p2, $p3, $p4
            "SIZE_MB=$([math]::Round($d.Length/1MB,1))"
        } else {
            "FILE=$($d.Name)"
            "PARSE_SKIP=unknown signature $sig"
        }
    }
}

# ===== Layer 3: WinDbg (optional) =====
$cdb = Get-Command cdb.exe -ErrorAction SilentlyContinue
if ($cdb -and $dumps.Count -gt 0) {
    $target = ($dumps | Select-Object -First 1).FullName
    $out = & $cdb -z $target -c "!analyze -v; q" 2>&1 | Out-String
    $m1 = [regex]::Match($out, 'Probably caused by\s*:\s*([^\r\n]+)')
    $m2 = [regex]::Match($out, 'FAILURE_BUCKET_ID\s*:\s*([^\r\n]+)')
    "[WINDBG]"
    "CAUSED_BY=$($m1.Groups[1].Value.Trim())"
    "BUCKET=$($m2.Groups[1].Value.Trim())"
}
