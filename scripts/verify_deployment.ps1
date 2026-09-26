# SisiCalendar 部署后端到端验证(PowerShell 5.1 / 7 通用)
#
# 用法:
#   $env:SISI_ADMIN_PASSWORD = '你的管理员口令'
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_deployment.ps1
#
# 说明:
#   - 全部 HTTP 调用走 curl.exe,避免 Windows PowerShell 5.1 发送非 ASCII 请求体时的编码问题。
#   - 脚本会创建一条测试日程,验证完成后自动删除,不留下数据。
#   - 口令只从环境变量读取,不写入本文件。

param(
  [string]$BaseUrl = 'http://localhost:443',
  [string]$Password = $env:SISI_ADMIN_PASSWORD,
  [string]$Username = 'admin'
)

$ErrorActionPreference = 'Stop'
$script:fail = 0
$script:pass = 0

function Step($name, $ok, $detail) {
  if ($ok) { $script:pass++ } else { $script:fail++ }
  "{0,-40} {1}  {2}" -f $name, $(if ($ok) { 'OK  ' } else { 'FAIL' }), $detail
}

function Read-Utf8($path) {
  return [System.IO.File]::ReadAllText($path, [System.Text.UTF8Encoding]::new($false))
}

function Invoke-HttpCheck {
  # 返回 @{ Status = <int>; Body = <string>; Headers = @{} }
  param([string[]]$Arguments, [string]$OutFile, [string]$HeaderFile)
  $argv = @('-s', '--max-time', '30', '-o', $OutFile, '-w', '%{http_code}')
  if ($HeaderFile) { $argv += @('-D', $HeaderFile) }
  $argv += $Arguments
  $code = & curl.exe @argv
  $headers = @{}
  if ($HeaderFile -and (Test-Path $HeaderFile)) {
    foreach ($line in [System.IO.File]::ReadAllLines($HeaderFile)) {
      $idx = $line.IndexOf(':')
      if ($idx -gt 0) { $headers[$line.Substring(0, $idx).Trim()] = $line.Substring($idx + 1).Trim() }
    }
  }
  return [pscustomobject]@{
    Status  = [int]$code
    Body    = $(if (Test-Path $OutFile) { Read-Utf8 $OutFile } else { '' })
    Headers = $headers
  }
}

if (-not $Password) {
  Write-Host '缺少管理员口令。请先设置环境变量 SISI_ADMIN_PASSWORD,或使用 -Password 参数。' -ForegroundColor Red
  exit 2
}

$work = Join-Path $env:TEMP ("sisi-verify-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$jar = Join-Path $work 'cookies.txt'
$hdrs = Join-Path $work 'headers.txt'
$eventId = ''

function Get-CsrfFromHtml($path, $pattern) {
  return [regex]::Match((Read-Utf8 $path), $pattern).Groups[1].Value
}

try {
  "=== 1. 健康检查 ==="
  $h = Invoke-HttpCheck @("$BaseUrl/healthz") (Join-Path $work 'health.json')
  Step 'GET /healthz' ($h.Status -eq 200 -and $h.Body -match '"status":"ok"') $h.Body.Trim()
  $r = Invoke-HttpCheck @("$BaseUrl/readyz") (Join-Path $work 'ready.json')
  Step 'GET /readyz' ($r.Status -eq 200 -and $r.Body -match '"status":"ok"') $r.Body.Trim()

  "`n=== 2. 订阅入口(未登录即可访问) ==="
  $feed = Invoke-HttpCheck @("$BaseUrl/calendar") (Join-Path $work 'feed.ics') $hdrs
  Step 'GET /calendar 返回 200' ($feed.Status -eq 200) "$($feed.Status)"
  Step 'Content-Type 正确' ($feed.Headers['Content-Type'] -eq 'text/calendar; charset=utf-8') $feed.Headers['Content-Type']
  Step 'ETag 存在' ([bool]$feed.Headers['ETag']) $feed.Headers['ETag']
  Step 'Cache-Control 正确' ($feed.Headers['Cache-Control'] -eq 'private, max-age=300, must-revalidate') $feed.Headers['Cache-Control']
  Step 'Content-Disposition inline' ($feed.Headers['Content-Disposition'] -eq 'inline; filename="calendar.ics"') $feed.Headers['Content-Disposition']
  $alias = Invoke-HttpCheck @("$BaseUrl/calendar.ics") (Join-Path $work 'alias.ics')
  Step '别名 /calendar.ics 内容一致' ($alias.Body -eq $feed.Body) ''
  $head = Invoke-HttpCheck @('-I', "$BaseUrl/calendar") (Join-Path $work 'head.txt')
  Step 'HEAD 返回 200' ($head.Status -eq 200) "$($head.Status)"
  Step 'ICS 结构完整且无 METHOD' ($feed.Body -match 'BEGIN:VCALENDAR' -and $feed.Body -match 'END:VCALENDAR' -and -not ($feed.Body -match 'METHOD:')) ''

  "`n=== 3. 未授权访问被拒绝 ==="
  $anon = Invoke-HttpCheck @("$BaseUrl/api/v1/events") (Join-Path $work 'anon.json')
  Step '/api/v1/events -> 401' ($anon.Status -eq 401) "$($anon.Status)"
  $anonSync = Invoke-HttpCheck @("$BaseUrl/api/v1/sync/status") (Join-Path $work 'anonsync.json')
  Step '/api/v1/sync/status -> 401' ($anonSync.Status -eq 401) "$($anonSync.Status)"
  $anonAdmin = Invoke-HttpCheck @("$BaseUrl/admin") (Join-Path $work 'admin_anon.html') $hdrs
  Step '/admin -> 303 跳转登录' ($anonAdmin.Status -eq 303 -and $anonAdmin.Headers['Location'] -like '/login*') "$($anonAdmin.Status) $($anonAdmin.Headers['Location'])"

  "`n=== 4. 登录 ==="
  $null = Invoke-HttpCheck @('-c', $jar, "$BaseUrl/login") (Join-Path $work 'login.html')
  $csrf = Get-CsrfFromHtml (Join-Path $work 'login.html') 'name="csrf_token" value="([^"]+)"'
  Step '登录页取得 CSRF 令牌' ([bool]$csrf) "$($csrf.Length) 字符"

  $login = Invoke-HttpCheck @(
    '-b', $jar, '-c', $jar, '-X', 'POST', "$BaseUrl/login",
    '--data-urlencode', "username=$Username",
    '--data-urlencode', "password=$Password",
    '--data-urlencode', "csrf_token=$csrf",
    '--data-urlencode', 'next=/admin'
  ) (Join-Path $work 'login.out') $hdrs
  Step 'POST /login -> 303' ($login.Status -eq 303) "$($login.Status) -> $($login.Headers['Location'])"

  $admin = Invoke-HttpCheck @('-b', $jar, '-c', $jar, "$BaseUrl/admin") (Join-Path $work 'admin.html')
  $apiCsrf = Get-CsrfFromHtml (Join-Path $work 'admin.html') 'data-csrf="([^"]+)"'
  Step 'GET /admin 渲染日历视图' ($admin.Status -eq 200 -and $admin.Body -match 'fullcalendar') "$($admin.Status)"

  "`n=== 5. 新建日程(含中文与 UTC 换算) ==="
  $title = "部署验证 $(Get-Date -Format 'HHmmss')"
  $payload = @{
    title       = $title
    location    = '本机容器'
    timezone    = 'Australia/Brisbane'
    description = '由 scripts/verify_deployment.ps1 创建,验证后自动删除。'
    start_at    = '2026-10-05T09:00:00'
    end_at      = '2026-10-05T10:30:00'
  } | ConvertTo-Json -Compress
  $payloadFile = Join-Path $work 'payload.json'
  [System.IO.File]::WriteAllText($payloadFile, $payload, [System.Text.UTF8Encoding]::new($false))

  $create = Invoke-HttpCheck @(
    '-b', $jar, '-H', "X-CSRF-Token: $apiCsrf",
    '-H', 'Content-Type: application/json; charset=utf-8',
    '--data-binary', "@$payloadFile", "$BaseUrl/api/v1/events"
  ) (Join-Path $work 'create.json')
  Step 'POST /api/v1/events -> 201' ($create.Status -eq 201) "status=$($create.Status)"
  if ($create.Status -ne 201) { "  请求体: $payload"; "  响应: $($create.Body)" }
  $created = $create.Body | ConvertFrom-Json
  $eventId = $created.id
  Step 'ICS 同步成功' ($created.sync.ok -eq $true) "version=$($created.sync.version)"
  Step '中文标题正确落库' ($created.title -eq $title) "$($created.title)"
  Step '本地时间到 UTC 换算正确' ($created.start_at_utc -eq '2026-10-04T23:00:00Z') "$($created.start_at_utc)"

  "`n=== 6. 订阅内容与条件请求 ==="
  $etagFile = Join-Path $work 'etag.txt'
  $feed2 = Invoke-HttpCheck @('--etag-save', $etagFile, "$BaseUrl/calendar") (Join-Path $work 'feed2.ics') $hdrs
  Step 'ICS 含新事件标题' ($feed2.Body -match [regex]::Escape($title)) ''
  Step '含 VTIMEZONE 与 TZID' ($feed2.Body -match 'BEGIN:VTIMEZONE' -and $feed2.Body -match 'TZID=Australia/Brisbane') ''
  Step 'CRLF 行结束符' ($feed2.Body -match "`r`n") ''
  Step 'ETag 随内容变化' ($feed2.Headers['ETag'] -ne $feed.Headers['ETag']) ''

  $lastMod = $feed2.Headers['Last-Modified']
  # ETag 本身带双引号,交给 curl 保存与回送,避免 PowerShell 原生参数丢失引号
  $cond = Invoke-HttpCheck @('--etag-compare', $etagFile, "$BaseUrl/calendar") (Join-Path $work 'cond.ics')
  Step 'If-None-Match -> 304' ($cond.Status -eq 304) "$($cond.Status)"
  $cond2 = Invoke-HttpCheck @('-H', "If-Modified-Since: $lastMod", "$BaseUrl/calendar") (Join-Path $work 'cond2.ics')
  Step 'If-Modified-Since -> 304' ($cond2.Status -eq 304) "$($cond2.Status)"

  "`n=== 7. 查询与同步状态 ==="
  $list = Invoke-HttpCheck @('-b', $jar, "$BaseUrl/api/v1/events?start=2026-10-01&end=2026-10-31") (Join-Path $work 'list.json')
  $listJson = $list.Body | ConvertFrom-Json
  Step '范围查询命中事件' ($listJson.total -ge 1) "total=$($listJson.total)"
  $status = Invoke-HttpCheck @('-b', $jar, "$BaseUrl/api/v1/sync/status") (Join-Path $work 'status.json')
  $statusJson = $status.Body | ConvertFrom-Json
  Step '同步状态: 未待修复' ($statusJson.dirty -eq $false) "version=$($statusJson.version) size=$($statusJson.size_bytes)"
  Step '同步状态: 发布文件存在' ($statusJson.file_exists -eq $true) ''
  Step '同步状态: SMTP 未配置' ($statusJson.mail_configured -eq $false) "收件人 $($statusJson.recipients_total) 位"

  "`n=== 8. 管理页面渲染 ==="
  foreach ($p in @('/admin/settings', '/admin/sync', '/admin/events/new')) {
    $page = Invoke-HttpCheck @('-b', $jar, "$BaseUrl$p") (Join-Path $work 'page.html')
    Step "GET $p" ($page.Status -eq 200) "$($page.Status)"
  }
  $edit = Invoke-HttpCheck @('-b', $jar, "$BaseUrl/admin/events/$eventId") (Join-Path $work 'edit.html')
  Step 'GET /admin/events/{id}' ($edit.Status -eq 200 -and $edit.Body -match [regex]::Escape($title)) ''

  "`n=== 9. 清理测试数据 ==="
  $del = Invoke-HttpCheck @('-b', $jar, '-X', 'DELETE', '-H', "X-CSRF-Token: $apiCsrf", "$BaseUrl/api/v1/events/$eventId") (Join-Path $work 'delete.json')
  $delJson = $del.Body | ConvertFrom-Json
  Step 'DELETE 软删除' ($del.Status -eq 200 -and $delJson.ok -eq $true) "version=$($delJson.sync.version)"
  $feed3 = Invoke-HttpCheck @("$BaseUrl/calendar") (Join-Path $work 'feed3.ics')
  Step '删除后从订阅移除' (-not ($feed3.Body -match [regex]::Escape($title))) ''
  Step '订阅仍为有效日历' ($feed3.Body -match 'END:VCALENDAR') "$($feed3.Body.Length) 字节"
  $eventId = ''
}
finally {
  if ($eventId) {
    "`n注意: 清理未完成,测试事件仍存在(id=$eventId),可在管理界面手动删除。"
  }
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

"`n================ 结果 ================"
"通过 $script:pass 项,失败 $script:fail 项"
if ($script:fail -gt 0) { exit 1 }
