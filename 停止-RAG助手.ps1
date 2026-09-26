# 停止-RAG助手.ps1 —— 只停止本包启动的进程（42 号窄修后版本）
#
# 身份核验（回应 42 号 P1：PID 复用会误杀无关 Python）：
#   - .run\*.pid 里存的是启动时写下的身份三元组：PID + 创建时间 + 完整命令行 + 标记；
#   - 停止前按 PID 重查现场进程，**三项全部对上**（PID 存在、命令行逐字一致、
#     创建时间与记录相差 ≤ 2 秒）才 Stop；
#   - 任何一项对不上 → **拒绝停止并保留 PID 文件作为证据**，绝不误杀；
#   - 停止后等待进程真正退出（最多 10 秒），再确认两个端口已无监听，
#     消除"脚本返回 0 但端口还活着"的空窗（42 号复跑 14/15 的空窗）。
#
# 不做端口扫描杀进程、不动其它任何程序。

$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-RecordSchema($record, $expectedMarker) {
    if (-not $record -or $record -isnot [pscustomobject]) { return $false }
    foreach ($field in @('pid', 'created', 'cmdline', 'marker')) {
        if ($record.PSObject.Properties.Name -notcontains $field) { return $false }
    }
    if (-not ($record.pid -is [int] -or $record.pid -is [long]) -or [long]$record.pid -le 0) {
        return $false
    }
    foreach ($field in @('created', 'cmdline', 'marker')) {
        if ($record.$field -isnot [string] -or [string]::IsNullOrWhiteSpace($record.$field)) {
            return $false
        }
    }
    if ($record.marker -ne $expectedMarker) { return $false }
    try {
        $null = [datetime]::ParseExact(
            $record.created,
            'yyyy-MM-dd HH:mm:ss.fff',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None
        )
    } catch {
        return $false
    }
    return $true
}

function Test-Identity($record, $ci, $expectedMarker) {
    if (-not (Test-RecordSchema $record $expectedMarker)) { return $false }
    if (-not $ci) { return $false }
    if ([int]$ci.ProcessId -ne [int]$record.pid) { return $false }
    if ([string]::IsNullOrWhiteSpace($ci.CommandLine)) { return $false }
    if ($ci.CommandLine -ne $record.cmdline) { return $false }
    if ($ci.CommandLine -notmatch [regex]::Escape($expectedMarker)) { return $false }
    $then = [datetime]::ParseExact(
        $record.created,
        'yyyy-MM-dd HH:mm:ss.fff',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None
    )
    $now = $ci.CreationDate
    if ([math]::Abs(($now - $then).TotalSeconds) -gt 2) { return $false }
    return $true
}

$stopped = 0
$hadError = $false
foreach ($name in @('api.pid', 'web.pid')) {
    $pidFile = Join-Path $Root ".run\$name"
    if (-not (Test-Path $pidFile)) { continue }
    $expectedMarker = if ($name -eq 'api.pid') { 'api_v3.py' } else { 'http.server' }
    $record = $null
    $record = Get-Content $pidFile -Raw | ConvertFrom-Json
    if (-not (Test-RecordSchema $record $expectedMarker)) {
        Write-Host "[停止] 拒绝使用 $name：身份记录缺字段、类型/日期错误或标记不符。PID 文件已保留作证据。" -ForegroundColor Yellow
        $hadError = $true
        continue
    }

    $ci = Get-CimInstance Win32_Process -Filter "ProcessId = $($record.pid)"
    if (-not $ci) {
        Write-Host "[停止] PID $($record.pid) 已不存在（可能是此前已停止），清理记录。"
        Remove-Item $pidFile -Force
        continue
    }

    if (-not (Test-Identity $record $ci $expectedMarker)) {
        # 42 号要求：身份不一致时保留 PID 证据并拒绝误杀
        Write-Host "[停止] 拒绝停止 PID $($record.pid)：现场进程身份与启动记录不一致（" `
            "现在：$($ci.CommandLine)；记录：$($record.cmdline)）。PID 文件已保留作证据。" -ForegroundColor Yellow
        $hadError = $true
        continue
    }

    Stop-Process -Id $record.pid -Force
    # 等待真正退出（最多 10 秒），消除"返回成功但端口还在"的空窗
    $gone = $false
    foreach ($i in 1..50) {
        if (-not (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) { $gone = $true; break }
        Start-Sleep -Milliseconds 200
    }
    if ($gone) {
        Write-Host "[停止] 已停止本包进程（PID $($record.pid)，来自 $name；已确认退出）。"
        Remove-Item $pidFile -Force
        $stopped++
    } else {
        Write-Host "[停止] PID $($record.pid) 已发送停止指令但 10 秒内未退出，请手工核对。" -ForegroundColor Yellow
        $hadError = $true
    }
}

# 端口确认：本包的两个监听端口都应已释放
foreach ($port in @(5280, 5273)) {
    $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listen) {
        Write-Host "[停止] 注意：端口 $port 仍有监听（PID $($listen.OwningProcess)）。" `
            "若不是本包记录的进程，请自行核对，本脚本不会去动它。" -ForegroundColor Yellow
        $hadError = $true
    }
}

if ($stopped -eq 0) {
    Write-Host '[停止] 没有可停止的本包进程（可能本来就没在运行，或身份核验拒绝了停止）。'
} else {
    Write-Host "[停止] 完成，共停止 $stopped 个进程（均已确认退出）。"
}

if ($hadError) { exit 1 }
exit 0
