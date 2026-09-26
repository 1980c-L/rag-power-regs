# 启动-RAG助手.ps1 —— Windows 本地便携包一键启动（40 号方案第 2 步；42 号窄修后版本）
#
# 行为边界：
#   - 只启动两个本机进程：api_v3.py（127.0.0.1:5280）与静态文件服务（127.0.0.1:5273，服务 dist-api）；
#   - 不写注册表、不开机自启、不监听外网；
#   - 每个进程在 .run\ 下记录**可验证身份**：PID + 创建时间 + 完整命令行（JSON）。
#     停止-RAG助手.ps1 会把三项全部核对一致才停，防 PID 复用误杀无关 Python（42 号 P1）；
#   - 没有配置 DEEPSEEK_API_KEY 也能用：默认只检索，不发任何模型请求。
#
# 前提：机器上有 Python 3.10+（并已 pip install -r requirements-api.txt）。

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 找 Python（优先 python，其次 py）
$Python = $null
foreach ($cand in @('python', 'py')) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $Python = $cand; break }
}
if (-not $Python) {
    Write-Host '[启动] 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。' -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
New-Item -ItemType Directory -Force -Path "$Root\.run"   | Out-Null

# 端口占用检查：被占用时如实说明，不抢、不重复启动
foreach ($port in @(5280, 5273)) {
    $busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "[启动] 端口 $port 已被占用。若本助手已在运行，请直接用浏览器打开；或先运行 停止-RAG助手.ps1。" -ForegroundColor Yellow
        exit 2
    }
}

# 记录可验证身份：PID + 创建时间 + 完整命令行（42 号窄修要求）
function Save-Identity($procId, $marker, $file) {
    $ci = Get-CimInstance Win32_Process -Filter "ProcessId = $procId"
    if (-not $ci) { throw "无法读取进程 $procId 的身份信息" }
    [pscustomobject]@{
        pid     = [int]$procId
        created = $ci.CreationDate.ToString('yyyy-MM-dd HH:mm:ss.fff')
        cmdline = $ci.CommandLine
        marker  = $marker
    } | ConvertTo-Json -Compress | Set-Content -Path $file -Encoding ascii
}

# 1) 启动本机 API
$api = Start-Process -FilePath $Python -ArgumentList @('api_v3.py', '--port', '5280') `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$Root\logs\api_out.log" -RedirectStandardError "$Root\logs\api_err.log"
Save-Identity $api.Id 'api_v3.py' "$Root\.run\api.pid"

# 2) 启动页面静态服务（只服务本包预构建的 api 版前端）
$web = Start-Process -FilePath $Python `
    -ArgumentList @('-m', 'http.server', '5273', '--bind', '127.0.0.1', '--directory', 'frontend_v3\dist-api') `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput "$Root\logs\web_out.log" -RedirectStandardError "$Root\logs\web_err.log"
Save-Identity $web.Id 'http.server' "$Root\.run\web.pid"

# 3) 等两个服务就绪（各最多 30 秒）
function Wait-Url($url) {
    foreach ($i in 1..60) {
        try {
            $null = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            return $true
        } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

$okApi = Wait-Url 'http://127.0.0.1:5280/api/health'
$okWeb = Wait-Url 'http://127.0.0.1:5273/'

if (-not $okApi -or -not $okWeb) {
    # 部分失败也要收拾干净：不留半启动的孤儿进程
    if (-not $okApi) { Write-Host '[启动] 本机 API 未就绪。日志：logs\api_err.log' -ForegroundColor Red }
    if (-not $okWeb) { Write-Host '[启动] 页面服务未就绪。日志：logs\web_err.log' -ForegroundColor Red }
    # 失败恢复也复用同一套严格身份核对；不再只按 PID + 进程名停止。
    & "$Root\停止-RAG助手.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[启动] 自清理未能完整确认；已保留 .run 身份记录，请手工核对。' -ForegroundColor Yellow
    }
    exit 3
}

Write-Host '[启动] 本机 API：http://127.0.0.1:5280（只绑本机回环）' -ForegroundColor Green
Write-Host '[启动] 页面地址：http://127.0.0.1:5273' -ForegroundColor Green
$key = [bool]($env:DEEPSEEK_API_KEY)
if ($key) {
    Write-Host '[启动] 已检测到 DEEPSEEK_API_KEY：页面里选"检索并生成"才会调用模型。' -ForegroundColor Green
} else {
    Write-Host '[启动] 未检测到 DEEPSEEK_API_KEY：只提供检索结果，不会发任何模型请求。' -ForegroundColor Green
}

# 4) 打开浏览器（设 RAG_NO_BROWSER=1 可跳过：自动化验收用，不打扰前台）
if (-not $env:RAG_NO_BROWSER) {
    Start-Process 'http://127.0.0.1:5273/'
}
Write-Host '[启动] 完成。停止请运行 停止-RAG助手.ps1。'
