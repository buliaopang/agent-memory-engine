<#
AME Phase 1.1 + 1.2 - Windows 实机验收脚本
用法（在项目根目录的 PowerShell 中）:
    powershell -ExecutionPolicy Bypass -File scripts\windows_acceptance.ps1
结果写入项目根目录 windows_acceptance_result.txt，请把这个文件发回。
脚本只在临时目录里建测试数据库，不会碰你的真实 AME 数据。
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$out = Join-Path $root "windows_acceptance_result.txt"
$work = Join-Path $env:TEMP ("AME 验收 " + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$results = @()

function Log($text) { Add-Content -Path $out -Value $text -Encoding UTF8 }
function Step($id, $name, [scriptblock]$block) {
    Log "`n===== [$id] $name ====="
    try {
        $output = & $block 2>&1 | Out-String
        $code = $LASTEXITCODE
    } catch {
        $output = $_ | Out-String
        $code = 999
    }
    Log $output
    return @{ id = $id; name = $name; output = $output; code = $code }
}
function Record($r, [bool]$ok) {
    $s = if ($ok) { "PASS" } else { "FAIL" }
    Log "[$($r.id)] 结果: $s (exit=$($r.code))"
    $script:results += "[$($r.id)] $s  $($r.name)"
}

Set-Content -Path $out -Value "AME Phase 1.1 + 1.2 Windows 验收结果" -Encoding UTF8
Log ("时间: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
Log ("系统: " + [System.Environment]::OSVersion.VersionString + " / PowerShell " + $PSVersionTable.PSVersion)
Log ("工作目录(临时): " + $work)

$py = ".venv\Scripts\python.exe"
$ame = ".venv\Scripts\ame.exe"

# W01 环境：Python 与 SQLite 版本（需要 SQLite >= 3.35）
$r = Step "W01" "Python / uv / SQLite 版本" {
    uv --version
    uv run python -c "import sys,sqlite3;print(sys.version);print('sqlite',sqlite3.sqlite_version);assert sqlite3.sqlite_version_info>=(3,35,0)"
}
Record $r ($r.code -eq 0)

# W02 安装依赖
$r = Step "W02" "uv sync 安装依赖" { uv sync --extra dev }
Record $r ($r.code -eq 0)

# W03 全量测试
$r = Step "W03" "pytest 全量测试" { & $py -m pytest -q }
Record $r (($r.code -eq 0) -and ($r.output -match "passed") -and ($r.output -notmatch "failed"))

# W04 ruff
$r = Step "W04" "ruff check + format --check" { & $py -m ruff check .; if ($LASTEXITCODE -eq 0) { & $py -m ruff format --check . } }
Record $r ($r.code -eq 0)

# W05 mypy
$r = Step "W05" "mypy --strict src" { & $py -m mypy --strict src }
Record $r ($r.code -eq 0)

# W06 中文+空格路径下迁移到最新版本
$db = Join-Path $work "数据 目录\ame.db"
$r = Step "W06" "中文/空格路径迁移到 head" { & $ame migrate --db "$db" }
Record $r (($r.code -eq 0) -and (Test-Path $db))

# W07 干净库 check-graph 应 exit 0
$r = Step "W07" "check-graph 干净库 (期望 exit 0)" { & $ame check-graph --db "$db" --verbose }
Record $r ($r.code -eq 0)

# W08 人为破坏后 check-graph 应 exit 2，且数据库文件不被修改
$broken = Join-Path $work "broken.db"
$r = Step "W08" "check-graph 损坏库 (期望 exit 2，只读)" {
    & $py scripts\make_broken_db.py "$broken"
    $h1 = (Get-FileHash $broken).Hash
    & $ame check-graph --db "$broken" --verbose
    $rc = $LASTEXITCODE
    $h2 = (Get-FileHash $broken).Hash
    "hash_unchanged=" + ($h1 -eq $h2)
    $global:LASTEXITCODE = $rc
}
Record $r (($r.code -eq 2) -and ($r.output -match "G001") -and ($r.output -match "hash_unchanged=True"))

# W09 备份与恢复
$bk = Join-Path $work "备份"
$r = Step "W09" "ame backup / ame restore" {
    & $ame backup --db "$db" --dest "$bk"
    if ($LASTEXITCODE -ne 0) { return }
    $f = Get-ChildItem -Path $bk -Filter *.db | Select-Object -First 1
    & $ame restore "$($f.FullName)" --db "$db" --yes
    if ($LASTEXITCODE -eq 0) { & $ame check-graph --db "$db" }
}
Record $r ($r.code -eq 0)

# W10 WAL 中未落盘数据也能被备份（专项测试）
$r = Step "W10" "WAL 备份专项测试" { & $py -m pytest -q tests/phase11/test_backup_restore.py }
Record $r ($r.code -eq 0)

# W11 并发写入与迁移失败自动恢复（专项测试）
$r = Step "W11" "并发写入 + 迁移失败自动恢复" {
    & $py -m pytest -q tests/phase11/test_dag_db_guards.py::test_a8_concurrent_opposite_edges_never_form_a_cycle tests/phase11/test_migration_upgrade.py
}
Record $r ($r.code -eq 0)

# ---------------- Phase 1.2 ----------------
# W12 Phase 1.2 专项测试（迁移 0006、空间、拓扑排序、REST、Phase 1 总验收）
$r = Step "W12" "Phase 1.2 专项测试 (tests/phase12)" { & $py -m pytest -q tests/phase12 }
Record $r ($r.code -eq 0)

# W13 中文/空格路径下 graph-order（只读）
$r = Step "W13" "graph-order 中文/空格路径 (期望 exit 0)" { & $ame graph-order --db "$db" --json }
Record $r ($r.code -eq 0)

# W14 中文/空格路径下创建/列出空间
$r = Step "W14" "space create/list 中文/空格路径" {
    & $ame space --db "$db" create "project:yanshou" --scope-type project --name "验收 空间"
    if ($LASTEXITCODE -eq 0) { & $ame space --db "$db" list }
}
Record $r (($r.code -eq 0) -and ($r.output -match "project:yanshou"))

# W15 真实启动服务：令牌文件、health 免认证、无令牌 401、带令牌 201、令牌不进日志
$srvData = Join-Path $work "服务 数据"
$srvLog = Join-Path $work "server.log"
$r = Step "W15" "REST 服务实机冒烟 (127.0.0.1:18765)" {
    $env:AME_DATA_DIR = $srvData
    $env:AME_PORT = "18765"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srvLog -RedirectStandardError "$srvLog.err"
    $ok = $true
    try {
        Start-Sleep -Seconds 6
        # Phase 3a（ADR-026）：Windows 上令牌以 DPAPI 加密保存为 api_token.dpapi，不再有明文文件；
        # 读令牌改用 `ame token show`（同一 Windows 用户才能解密）。未在实机验证。
        $tokFile = Join-Path $srvData "api_token.dpapi"
        if (-not (Test-Path $tokFile)) { "api_token.dpapi 未生成"; $ok = $false }
        if (Test-Path (Join-Path $srvData "api_token")) { "仍有明文 api_token 文件！"; $ok = $false }
        $tok = (& $ame token show | Select-Object -Last 1).Trim()
        "令牌文件权限(icacls，仅记录):"
        icacls $tokFile
        $base = "http://127.0.0.1:18765/api/v1"
        $h = Invoke-WebRequest -UseBasicParsing "$base/health"
        "health=" + $h.StatusCode; if ($h.StatusCode -ne 200) { $ok = $false }
        $body = '{"content":"hi","memory_type":"semantic","layer":"L2","scope_type":"shared","scope_id":"shared","importance":0.5,"confidence":0.5}'
        try { Invoke-WebRequest -UseBasicParsing -Method Post "$base/memories" -ContentType "application/json" -Body $body | Out-Null; "noauth=未拒绝"; $ok = $false }
        catch { $c = [int]$_.Exception.Response.StatusCode; "noauth=" + $c; if ($c -ne 401) { $ok = $false } }
        $cr = Invoke-WebRequest -UseBasicParsing -Method Post "$base/memories" -ContentType "application/json" -Body $body -Headers @{ Authorization = "Bearer $tok" }
        "create=" + $cr.StatusCode; if ($cr.StatusCode -ne 201) { $ok = $false }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    $logText = (Get-Content $srvLog -Raw -ErrorAction SilentlyContinue) + (Get-Content "$srvLog.err" -Raw -ErrorAction SilentlyContinue)
    if ($tok -and $logText -and $logText.Contains($tok)) { "令牌出现在日志中！"; $ok = $false }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W15_OK" } else { "W15_FAIL" }
}
Record $r ($r.output -match "W15_OK")

# ---------------- Phase 2（检索；以下各项从未在 Windows 实机运行过）----------------
# W16 Phase 2 专项测试（迁移 0007、中文关键词搜索、打分、图扩展、上下文预算、Outbox、降级）
$r = Step "W16" "Phase 2 专项测试 (tests/phase2)" { & $py -m pytest -q tests/phase2 }
Record $r ($r.code -eq 0)

# W17 中文/空格路径下 index 命令（status / run / rebuild-lexical）
$r = Step "W17" "ame index 中文/空格路径" {
    & $ame index status --db "$db" --json
    if ($LASTEXITCODE -eq 0) { & $ame index run --db "$db" }
    if ($LASTEXITCODE -eq 0) { & $ame index rebuild-lexical --db "$db" }
    if ($LASTEXITCODE -eq 0) { & $ame check-graph --db "$db" }
}
Record $r ($r.code -eq 0)

# W18 真实服务：中文搜索（UTF-8 请求体）、retrieval_mode=lexical_graph、后台 worker running、查询词不进日志
$srv2Data = Join-Path $work "服务 数据2"
$srv2Log = Join-Path $work "server2.log"
$r = Step "W18" "中文搜索实机冒烟 (127.0.0.1:18765)" {
    $env:AME_DATA_DIR = $srv2Data
    $env:AME_PORT = "18765"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srv2Log -RedirectStandardError "$srv2Log.err"
    $ok = $true
    $q = "例会验收词"
    try {
        Start-Sleep -Seconds 6
        $tok = (& $ame token show | Select-Object -Last 1).Trim()  # Phase 3a：DPAPI，见 W15
        $hd = @{ Authorization = "Bearer $tok" }
        $base = "http://127.0.0.1:18765/api/v1"
        $ct = "application/json; charset=utf-8"
        $body = '{"content":"每周三开' + $q + '","memory_type":"semantic","layer":"L2","scope_type":"shared","scope_id":"shared","importance":0.5,"confidence":0.5}'
        $cr = Invoke-WebRequest -UseBasicParsing -Method Post "$base/memories" -ContentType $ct -Body ([Text.Encoding]::UTF8.GetBytes($body)) -Headers $hd
        "create=" + $cr.StatusCode; if ($cr.StatusCode -ne 201) { $ok = $false }
        $sb = '{"query":"' + $q + '"}'
        $sr = Invoke-WebRequest -UseBasicParsing -Method Post "$base/memories/search" -ContentType $ct -Body ([Text.Encoding]::UTF8.GetBytes($sb)) -Headers $hd
        $txt = [Text.Encoding]::UTF8.GetString($sr.RawContentStream.ToArray())
        $j = $txt | ConvertFrom-Json
        "mode=" + $j.retrieval_mode + " results=" + $j.results.Count
        if ($j.retrieval_mode -ne "lexical_graph" -or $j.results.Count -lt 1) { $ok = $false }
        elseif ($null -ne $j.results[0].score_breakdown.vector) { "vector 分不应有值"; $ok = $false }
        Start-Sleep -Seconds 4
        $h = (Invoke-WebRequest -UseBasicParsing "$base/health").Content | ConvertFrom-Json
        "worker=" + $h.worker.status + " pending=" + $h.worker.pending_events
        if ($h.worker.status -ne "running") { $ok = $false }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    $logText = (Get-Content $srv2Log -Raw -Encoding UTF8 -ErrorAction SilentlyContinue) + (Get-Content "$srv2Log.err" -Raw -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($logText -and $logText.Contains($q)) { "查询词/正文出现在日志中！"; $ok = $false }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W18_OK" } else { "W18_FAIL" }
}
Record $r ($r.output -match "W18_OK")

# ---- Phase 2.1（ADR-023，中文去空格索引；从未在 Windows 实机运行，结果未验证）----
# W19 去空格副本索引的专项测试（含 0007->0008 升级/降级/再升级）
$r = Step "W19" "中文去空格索引专项测试 tests/phase21" { & $py -m pytest -q tests/phase21 }
Record $r (($r.code -eq 0) -and ($r.output -match "passed") -and ($r.output -notmatch "failed"))

# W20 Windows 上 CRLF 换行/全角空格：'部署流程' 能搜到 '部署\r\n流程' 与 '部署　流程'
$r = Step "W20" "中文去空格搜索实机冒烟 (CRLF/全角空格)" {
    $code = @"
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from ame.config import Settings
from ame.infrastructure.database import create_sqlite_engine, run_migrations
from ame.infrastructure.unit_of_work import make_session_factory
from ame.application.memory_service import MemoryService
from ame.application.search_service import SearchService
from ame.application.schemas import SearchRequest
from ame.application.integrity_service import IntegrityService
from tests.conftest import USER, make
s = Settings(data_dir=Path(tempfile.mkdtemp()) / 'd', index_worker_enabled=False)
run_migrations(s)
sf = make_session_factory(create_sqlite_engine(s))
svc = MemoryService(sf)
a = svc.create_memory(make(content='部署\r\n流程'), USER).memory_id
b = svc.create_memory(make(content='部署\u3000流程'), USER).memory_id
got = {r.memory.memory_id for r in SearchService(sf).search(SearchRequest.model_validate({'query': '部署流程'}), datetime.now(UTC)).results}
rep = IntegrityService(s.db_path).run()
print('hits=', len(got), 'g013=', 'G013' in rep.checked, 'exit=', rep.exit_code)
print('W20_OK' if got == {a, b} and 'G013' in rep.checked and rep.exit_code == 0 else 'W20_FAIL')
"@
    $env:PYTHONIOENCODING = "utf-8"
    & $py -c $code
}
Record $r ($r.output -match "W20_OK")

# ---------------- Phase 3a（身份与权限；以下各项从未在 Windows 实机运行过，状态：未验证）----------------
# W21 Phase 3a 专项测试（0009 审计表复制扩容、0010 访问名单、权限矩阵、令牌轮换）
$r = Step "W21" "Phase 3a 专项测试 (tests/phase3a)" { & $py -m pytest -q tests/phase3a }
Record $r ($r.code -eq 0)

# W22 DPAPI：令牌只以密文落盘；token show 能解密；token rotate 后换新且旧值不再出现
$tkData = Join-Path $work "令牌 数据"
$r = Step "W22" "用户令牌 DPAPI 加密 + ame token rotate" {
    $env:AME_DATA_DIR = $tkData
    $ok = $true
    $t1 = (& $ame token show | Select-Object -Last 1).Trim()
    $blob = Join-Path $tkData "api_token.dpapi"
    if (-not (Test-Path $blob)) { "没有 api_token.dpapi"; $ok = $false }
    if (Test-Path (Join-Path $tkData "api_token")) { "存在明文 api_token"; $ok = $false }
    $raw = [IO.File]::ReadAllBytes($blob)
    $txt = [Text.Encoding]::ASCII.GetString($raw)
    if ($t1.Length -lt 32 -or $txt.Contains($t1)) { "密文中能看到明文令牌！"; $ok = $false }
    $rot = & $ame token rotate
    if (($rot | Out-String).Contains($t1)) { "rotate 输出了令牌"; $ok = $false }
    $t2 = (& $ame token show | Select-Object -Last 1).Trim()
    if ($t2 -eq $t1) { "rotate 后令牌没变"; $ok = $false }
    Remove-Item Env:AME_DATA_DIR -ErrorAction SilentlyContinue
    if ($ok) { "W22_OK" } else { "W22_FAIL" }
}
Record $r ($r.output -match "W22_OK")

# W23 中文/空格路径：agent 注册+发钥匙、空间授权/撤销、禁用、check-graph（含 G014）
$r = Step "W23" "ame agent / space grant 中文路径" {
    & $ame agent --db "$db" create "agent:yanshou" --agent-type test --name "验收 代理"
    if ($LASTEXITCODE -eq 0) { & $ame space --db "$db" grant "project:yanshou" "agent:yanshou" --role reader --reason "验收" }
    if ($LASTEXITCODE -eq 0) { & $ame space --db "$db" members "project:yanshou" }
    if ($LASTEXITCODE -eq 0) { & $ame space --db "$db" revoke "project:yanshou" "agent:yanshou" --reason "验收" }
    if ($LASTEXITCODE -eq 0) { & $ame agent --db "$db" disable "agent:yanshou" --reason "验收" }
    if ($LASTEXITCODE -eq 0) { & $ame check-graph --db "$db" --verbose }
}
Record $r (($r.code -eq 0) -and ($r.output -match "G014"))

# W24 真实服务：agent 钥匙写入=candidate；他人私有记忆 404；禁用后 401；钥匙不进日志
$srv3Data = Join-Path $work "服务 数据3"
$srv3Log = Join-Path $work "server3.log"
$r = Step "W24" "Agent 权限实机冒烟 (127.0.0.1:18765)" {
    $env:AME_DATA_DIR = $srv3Data
    $env:AME_PORT = "18765"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srv3Log -RedirectStandardError "$srv3Log.err"
    $ok = $true
    $key = $null
    try {
        Start-Sleep -Seconds 6
        $tok = (& $ame token show | Select-Object -Last 1).Trim()
        $u = @{ Authorization = "Bearer $tok" }
        $base = "http://127.0.0.1:18765/api/v1"
        $ct = "application/json; charset=utf-8"
        function J($m, $path, $hd, $obj) {
            $b = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Compress))
            try { $x = Invoke-WebRequest -UseBasicParsing -Method $m "$base$path" -ContentType $ct -Body $b -Headers $hd; return @($x.StatusCode, ($x.Content | ConvertFrom-Json)) }
            catch { return @([int]$_.Exception.Response.StatusCode, $null) }
        }
        J Post "/agents" $u @{ agent_id = "agent:w24"; display_name = "w24"; agent_type = "t" } | Out-Null
        $k = J Post "/agents/agent:w24/credentials" $u @{ reason = "验收" }
        $key = $k[1].api_key
        $a = @{ Authorization = "Bearer $key" }
        J Post "/spaces" $u @{ space_id = "private:w24"; name = "私有"; scope_type = "private" } | Out-Null
        $pm = J Post "/memories" $u @{ content = "私有 内容"; memory_type = "semantic"; layer = "L2"; scope_type = "private"; scope_id = "private:w24"; importance = 0.5; confidence = 0.5 }
        $am = J Post "/memories" $a @{ content = "代理 写入"; memory_type = "semantic"; layer = "L2"; scope_type = "shared"; scope_id = "shared"; importance = 0.5; confidence = 0.5 }
        "agent_create=" + $am[0] + " status=" + $am[1].status
        if ($am[0] -ne 201 -or $am[1].status -ne "candidate") { $ok = $false }
        try { Invoke-WebRequest -UseBasicParsing "$base/memories/$($pm[1].memory_id)" -Headers $a | Out-Null; "私有可见！"; $ok = $false }
        catch { $c = [int]$_.Exception.Response.StatusCode; "private_get=" + $c; if ($c -ne 404) { $ok = $false } }
        J Post "/agents/agent:w24/disable" $u @{ reason = "验收" } | Out-Null
        try { Invoke-WebRequest -UseBasicParsing "$base/stats" -Headers $a | Out-Null; "禁用后仍可访问！"; $ok = $false }
        catch { $c = [int]$_.Exception.Response.StatusCode; "disabled=" + $c; if ($c -ne 401) { $ok = $false } }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    $logText = (Get-Content $srv3Log -Raw -ErrorAction SilentlyContinue) + (Get-Content "$srv3Log.err" -Raw -ErrorAction SilentlyContinue)
    if ($key -and $logText -and $logText.Contains($key)) { "Agent 钥匙出现在日志中！"; $ok = $false }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W24_OK" } else { "W24_FAIL" }
}
Record $r ($r.output -match "W24_OK")

# W25 Phase 3b 专项测试（0011 幂等表、幂等键、多 Agent 并发写、审计序号无缺口）
$r = Step "W25" "Phase 3b 专项测试 (tests/phase3b)" { & $py -m pytest -q tests/phase3b }
Record $r ($r.code -eq 0)

# W26 真实服务：同键同内容只写一条并回放；同键不同内容 422；ame idempotency status
$srv4Data = Join-Path $work "服务 数据4"
$srv4Log = Join-Path $work "server4.log"
$r = Step "W26" "Idempotency-Key 实机冒烟 (127.0.0.1:18766)" {
    $env:AME_DATA_DIR = $srv4Data
    $env:AME_PORT = "18766"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srv4Log -RedirectStandardError "$srv4Log.err"
    $ok = $true
    try {
        Start-Sleep -Seconds 6
        $tok = (& $ame token show | Select-Object -Last 1).Trim()
        $base = "http://127.0.0.1:18766/api/v1"
        $ct = "application/json; charset=utf-8"
        function P($obj) {
            $h = @{ Authorization = "Bearer $tok"; "Idempotency-Key" = "w26-key" }
            $b = [Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Compress))
            try { $x = Invoke-WebRequest -UseBasicParsing -Method Post "$base/memories" -ContentType $ct -Body $b -Headers $h; return @($x.StatusCode, ($x.Content | ConvertFrom-Json), $x.Headers["Idempotency-Replayed"]) }
            catch { return @([int]$_.Exception.Response.StatusCode, $null, $null) }
        }
        $m = @{ content = "幂等 验收"; memory_type = "semantic"; layer = "L2"; scope_type = "shared"; scope_id = "shared"; importance = 0.5; confidence = 0.5 }
        $a = P $m
        $b2 = P $m
        "first=" + $a[0] + " second=" + $b2[0] + " replayed=" + $b2[2]
        if ($a[0] -ne 201 -or $b2[0] -ne 201 -or $a[1].memory_id -ne $b2[1].memory_id -or "$($b2[2])" -ne "true") { $ok = $false }
        $m.content = "不同 内容"
        $c = P $m
        "mismatch=" + $c[0]
        if ($c[0] -ne 422) { $ok = $false }
        $st = (& $ame idempotency status) -join " "
        $st
        if ($st -notmatch "done=1") { $ok = $false }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W26_OK" } else { "W26_FAIL" }
}
Record $r ($r.output -match "W26_OK")

# ===== Phase 3c（MCP，ADR-028）：W27–W30 均【未在 Windows 验证】 =====
# W27 安装 mcp 可选依赖（含 pywin32）后跑 tests/phase3c
$r = Step "W27" "安装 ame[mcp] 并运行 tests/phase3c" {
    uv sync --extra dev --extra mcp
    & $py -c "import mcp, importlib.metadata as m; print('mcp', m.version('mcp'))"
    & $py -m pytest -q tests/phase3c
}
Record $r ($r.code -eq 0)

# W28 ame mcp setup：钥匙以 DPAPI 加密保存（.key.dpapi，无明文 .key），屏幕输出不含钥匙
$mcpData = Join-Path $work "MCP 数据"
$r = Step "W28" "ame mcp setup 钥匙 DPAPI 保存" {
    $env:AME_DATA_DIR = $mcpData
    $ok = $true
    & $ame migrate | Out-Null
    & $ame agent create agent:w28 --agent-type t --no-key | Out-Null
    $out = (& $ame mcp setup --agent agent:w28) -join "`n"
    $out
    $blob = Join-Path $mcpData "mcp\agent_w28.key.dpapi"
    $plain = Join-Path $mcpData "mcp\agent_w28.key"
    if (-not (Test-Path $blob)) { "没有 .key.dpapi"; $ok = $false }
    if (Test-Path $plain) { "存在明文 .key！"; $ok = $false }
    if ($out -match "ame_agt_") { "屏幕输出含钥匙！"; $ok = $false }
    if ((Test-Path $blob) -and ([IO.File]::ReadAllText($blob) -match "ame_agt_")) { "加密文件含明文！"; $ok = $false }
    Remove-Item Env:AME_DATA_DIR -ErrorAction SilentlyContinue
    if ($ok) { "W28_OK" } else { "W28_FAIL" }
}
Record $r ($r.output -match "W28_OK")

# W29 中文/空格路径下真实 stdio 子进程端到端（UTF-8 stdout、CRLF）
$r = Step "W29" "MCP stdio 端到端 (中文路径)" { & $py -m pytest -q tests/phase3c/test_mcp_sdk.py -k stdio }
Record $r ($r.code -eq 0)

# W30 人工清单：Claude Desktop / Codex / Cursor 按 docs\MCP_接入指南.md 接入（脚本无法自动判断）
$r = Step "W30" "客户端接入人工核对（MANUAL）" {
    "请人工核对：1) 客户端能列出 8 个只读工具（Phase 4 起；读写模式 18 个）；2) memory_search 返回带 AME_MEMORY_DATA 包裹；3) ame agent disable 后调用报 401。"
    "W30_MANUAL"
}
$script:results += "[W30] MANUAL（需人工核对，未自动判定）"

# ===== Phase 4（反馈 / Epoch / 主链 / 提炼 / 衰减，ADR-029–032）：W31–W38 均【未在 Windows 验证】 =====
# W31 中文/空格路径：迁移到 0015，审计行数与全列指纹不变（tests/phase4 迁移用例）
$r = Step "W31" "迁移到 0015 (中文路径，审计指纹不变)" { & $py -m pytest -q tests/phase4/test_phase4_infra.py -k "upgrade or failure or refuses" }
Record $r ($r.code -eq 0)

# W32 Phase 4 专项测试
$r = Step "W32" "Phase 4 专项测试 (tests/phase4)" { & $py -m pytest -q tests/phase4 }
Record $r ($r.code -eq 0)

# W33 真实服务：反馈写入并进审计；同一人重复反馈只算最近一次
$srv5Data = Join-Path $work "服务 数据5"
$srv5Log = Join-Path $work "server5.log"
$r = Step "W33" "反馈与审计实机冒烟 (127.0.0.1:18767)" {
    $env:AME_DATA_DIR = $srv5Data
    $env:AME_PORT = "18767"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srv5Log -RedirectStandardError "$srv5Log.err"
    $ok = $true
    try {
        Start-Sleep -Seconds 6
        $tok = (& $ame token show | Select-Object -Last 1).Trim()
        $base = "http://127.0.0.1:18767/api/v1"
        $ct = "application/json; charset=utf-8"
        $h = @{ Authorization = "Bearer $tok" }
        # 注意：用逗号包住，避免 PowerShell 把 byte[] 拆成 object[]（Windows 实机 22:04 发现）
        function J($obj) { return ,([Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Compress))) }
        $m = Invoke-RestMethod -Method Post "$base/memories" -ContentType $ct -Headers $h -Body (J @{ content = "反馈 验收"; memory_type = "semantic"; layer = "L2"; scope_type = "shared"; scope_id = "shared"; importance = 0.5; confidence = 0.5 })
        foreach ($k in @("useful", "wrong")) { Invoke-RestMethod -Method Post "$base/memories/$($m.memory_id)/feedback" -ContentType $ct -Headers $h -Body (J @{ feedback = $k; reason = "验收" }) | Out-Null }
        $s = Invoke-RestMethod "$base/memories/$($m.memory_id)/feedback" -Headers $h
        "voters=" + $s.voters + " wrong=" + $s.counts.wrong + " items=" + $s.items.Count
        if ($s.voters -ne 1 -or $s.counts.wrong -ne 1 -or $s.items.Count -ne 2) { $ok = $false }
        $a = Invoke-RestMethod "$base/memories/$($m.memory_id)/audit" -Headers $h
        if (-not ($a | Where-Object { $_.event_type -eq "FEEDBACK_ADDED" })) { "审计里没有 FEEDBACK_ADDED"; $ok = $false }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W33_OK" } else { "W33_FAIL" }
}
Record $r ($r.output -match "W33_OK")

# W34 Epoch 命令行（中文名称显示正常；关闭后不能再加成员）
$p4Db = Join-Path $work "第四阶段 数据\ame.db"
$r = Step "W34" "ame epoch（中文名称）" {
    $ok = $true
    # 控制台默认 gb2312，而 ame 按 UTF-8 输出（PYTHONIOENCODING），先切到 UTF-8 再比较
    $oldEnc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $ids = @(& $py scripts\phase4_seed.py $p4Db | Where-Object { $_ -match "^mem_" })
    $c = (& $ame epoch --db $p4Db create "2026 秋季 搬家" --reason "验收") -join " "
    $c
    $eid = ($c -split "\s+")[2]
    & $ame epoch --db $p4Db add $eid $ids[0] --reason "验收"
    $l = (& $ame epoch --db $p4Db list) -join "`n"
    $l
    if ($l -notmatch "2026 秋季 搬家") { "中文名称显示不正确"; $ok = $false }
    & $ame epoch --db $p4Db close $eid --reason "验收"
    & $ame epoch --db $p4Db add $eid $ids[1] --reason "关闭后" 2>&1
    if ($LASTEXITCODE -ne 3) { "关闭后仍能加成员"; $ok = $false }
    [Console]::OutputEncoding = $oldEnc
    if ($ok) { "W34_OK" } else { "W34_FAIL" }
}
Record $r ($r.output -match "W34_OK")

# W35 提炼 → 审核 → 晋升（中文正文）；原记忆不变，check-graph 通过
$r = Step "W35" "ame distill / review promote（中文）" {
    $ok = $true
    $d = (& $ame distill --db $p4Db run --scope shared --threshold 0.4 --reason "验收") -join "`n"
    $d
    if ($d -notmatch "created=1") { $ok = $false }
    $draft = (($d -split "`n") | Where-Object { $_ -match "^mem_" } | Select-Object -First 1) -split "\s+" | Select-Object -First 1
    $l = (& $ame review --db $p4Db list) -join "`n"
    $l
    if ($l -notmatch "distill") { $ok = $false }
    $pr = (& $ame review --db $p4Db promote $draft --reason "确认") -join " "
    $pr
    if ($pr -notmatch "active") { $ok = $false }
    & $ame check-graph --db $p4Db
    if ($LASTEXITCODE -ne 0) { $ok = $false }
    if ($ok) { "W35_OK" } else { "W35_FAIL" }
}
Record $r ($r.output -match "W35_OK")

# W36 衰减建议只读：运行前后数据库文件哈希一致
$r = Step "W36" "ame decay suggest（只读，文件哈希不变）" {
    $h1 = (Get-FileHash $p4Db -Algorithm SHA256).Hash
    & $ame decay --db $p4Db suggest --threshold 0.99
    $h2 = (Get-FileHash $p4Db -Algorithm SHA256).Hash
    "before=$h1 after=$h2"
    if ($h1 -eq $h2) { "W36_OK" } else { "W36_FAIL" }
}
Record $r ($r.output -match "W36_OK")

# W37 rotate 后不重启：旧令牌 401，新令牌 200
$srv6Data = Join-Path $work "服务 数据6"
$srv6Log = Join-Path $work "server6.log"
$r = Step "W37" "token rotate 不重启生效 (127.0.0.1:18768)" {
    $env:AME_DATA_DIR = $srv6Data
    $env:AME_PORT = "18768"
    $p = Start-Process -FilePath ".venv\Scripts\ame-server.exe" -PassThru -NoNewWindow -RedirectStandardOutput $srv6Log -RedirectStandardError "$srv6Log.err"
    $ok = $true
    try {
        Start-Sleep -Seconds 6
        $base = "http://127.0.0.1:18768/api/v1"
        $old = (& $ame token show | Select-Object -Last 1).Trim()
        & $ame token rotate | Out-Null
        $new = (& $ame token show | Select-Object -Last 1).Trim()
        function G($t) { try { (Invoke-WebRequest -UseBasicParsing "$base/spaces" -Headers @{ Authorization = "Bearer $t" }).StatusCode } catch { [int]$_.Exception.Response.StatusCode } }
        $a = G $old; $b = G $new
        "old=$a new=$b"
        if ($a -ne 401 -or $b -ne 200) { $ok = $false }
    } catch { "异常: " + ($_ | Out-String); $ok = $false }
    finally { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    Remove-Item Env:AME_DATA_DIR, Env:AME_PORT -ErrorAction SilentlyContinue
    if ($ok) { "W37_OK" } else { "W37_FAIL" }
}
Record $r ($r.output -match "W37_OK")

# W38 MCP 新工具（memory_feedback / memory_spine / memory_list_epochs）走真实 stdio
$r = Step "W38" "MCP 新工具 stdio + 内存客户端" { & $py -m pytest -q tests/phase3c/test_mcp_sdk.py tests/phase4/test_phase4_mcp.py }
Record $r ($r.code -eq 0)

Log "`n===== 汇总 ====="
$results | ForEach-Object { Log $_ }
$fail = ($results | Where-Object { $_ -match "FAIL" }).Count
Log ("总计: " + $results.Count + " 项, 失败 " + $fail + " 项")
Write-Host ""
$results | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "结果已写入: $out  —— 请把这个文件发回。"
