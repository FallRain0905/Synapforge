# Demo 1.0 端到端演示脚本（UX-7-05）
#
# 用途：把"一个没读过源码的人能否在 15 分钟内走通全链路"变成可重复执行的检查。
#
#   .\scripts\demo-1.0.ps1 -Api http://127.0.0.1:8000
#
# 脚本自动完成可 API 化的部分并断言结果；需要界面的部分打印"检查点"，由演示者在浏览器里做，
# 脚本随后回查平台状态确认是否真的生效（不做"看起来点了就算过"的判断）。
#
# 前置：API 已启动（scripts\start-demo.ps1）。Web 打开 http://127.0.0.1:3000

param(
    [string]$Api = 'http://127.0.0.1:8000',
    [string]$ProjectName = 'Demo 1.0 Project',
    [switch]$SkipWorker,
    # 无人值守：第 6 步用 API 代替人工点界面（仅用于自动化回归，不是演示路径）
    [switch]$AutoApprove
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot | Split-Path -Parent
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw 'python 未安装或不在 PATH 中' }
$env:PYTHONPATH = "$root;$root\apps\agent;$root\apps\api"

$script:checks = @()
function Check([string]$name, [bool]$ok, [string]$detail = '') {
    $script:checks += [pscustomobject]@{ name = $name; ok = $ok; detail = $detail }
    $mark = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Output ("  [{0}] {1}{2}" -f $mark, $name, $(if ($detail) { " — $detail" } else { '' }))
}

function Api([string]$method, [string]$path, $body = $null) {
    $params = @{ Uri = "$($Api.TrimEnd('/'))$path"; Method = $method; TimeoutSec = 30 }
    if ($body) {
        $params.Body = ($body | ConvertTo-Json -Depth 8)
        $params.ContentType = 'application/json; charset=utf-8'
    }
    return Invoke-RestMethod @params
}

Write-Output '=== 0/7 平台可达性 ==='
$health = Api 'GET' '/api/platform/health'
Check '平台健康检查' ($null -ne $health)

Write-Output '=== 1/7 新建项目（界面：总览页「新建项目」）==='
$project = Api 'POST' '/api/projects' @{ name = $ProjectName; competition_pack = 'cumcm-2026'; problem_code = 'C' }
Check '项目已创建' ($null -ne $project.id) $project.name
$projects = Api 'GET' '/api/projects'
$matched = @($projects | Where-Object { $_.id -eq $project.id })
Check '项目出现在列表（界面侧栏/切换器应能看到）' ($matched.Count -eq 1) "命中 $($matched.Count) 个同名项目"

Write-Output '=== 2/7 应用模板包（界面：建模模板包页 → 一键应用，需确认弹窗）==='
$apply = Api 'POST' "/api/projects/$($project.id)/competition-pack/apply" @{
    problem_code = 'C'; created_by = 'demo-1.0'; idempotency_key = "demo-apply-$($project.id)"
}
Check '模板包物化出任务与成果物' ($apply.created_task_count -gt 0 -and $apply.created_artifact_count -gt 0) `
    "$($apply.created_task_count) 任务 / $($apply.created_artifact_count) 成果物"

$dashboard = Api 'GET' "/api/projects/$($project.id)/dashboard"
Check '看板可见任务与成果物' ($dashboard.tasks.Count -gt 0 -and $dashboard.artifacts.Count -gt 0)

Write-Output '=== 3/7 建一个可执行任务（界面：任务页 → 新建任务）==='
$task = Api 'POST' "/api/projects/$($project.id)/tasks" @{
    title = '演示任务：执行声明式命令'
    description = 'worker 自动领取并执行'
    stage = 'coding'
    assignee = 'Unassigned'
    priority = 'critical'
    requires_review = $false
    allow_future_data = $false
    output_types = @()
    resource_policy = @{ worker_command = @($python, '-c', "print('demo-1.0 task executed')") }
}
Check '任务已创建并进入 READY' ($task.status -eq 'READY')

Write-Output '=== 4/7 接入 Agent（界面：设备与接入页 → 生成配对 → 复制命令执行）==='
$pairing = Api 'POST' '/api/devices/pairings' @{ organization_id = '00000000-0000-4000-8000-000000000001'; expires_in_seconds = 900 }
Check '配对已生成（15 分钟一次性）' ($null -ne $pairing.pairing_code)

$agentId = "agent-demo-$([guid]::NewGuid().ToString('N').Substring(0,6))"
$deviceId = "device-demo-$([guid]::NewGuid().ToString('N').Substring(0,6))"
Api 'POST' '/api/agents/register' @{ agent_id = $agentId; display_name = 'Demo Agent'; owner_member_id = 'member-001' } | Out-Null

$blob = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((@{
    pairing_id = $pairing.id; pairing_code = $pairing.pairing_code; challenge = $pairing.challenge; expires_at = $pairing.expires_at
} | ConvertTo-Json -Compress))).TrimEnd('=').Replace('+','-').Replace('/','_')

& (Join-Path $root 'scripts\connect-agent.ps1') -Url $Api -AgentName 'Demo 工作站' -AgentId $agentId -DeviceId $deviceId -Pairing $blob -SkipCredential | Out-Null
$devices = Api 'GET' '/api/devices'
$device = $devices | Where-Object { $_.device_id -eq $deviceId }
Check '设备已登记且状态 active' ($null -ne $device -and $device.status -eq 'active') $deviceId

Write-Output '=== 5/7 项目授权 + Agent 自动领任务（界面：设备页 → 授权到项目 → 复制 worker 命令）==='
$grant = Api 'POST' "/api/projects/$($project.id)/device-grants" @{ device_id = $deviceId }
Check '项目授权已签发（一次性 project_token）' ($null -ne $grant.project_token)

$grantBlob = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((@{
    project_id = $grant.grant.project_id; project_token = $grant.project_token; capabilities = $grant.grant.capabilities
    agent_id = $grant.grant.agent_id; device_id = $grant.grant.device_id; expires_at = $grant.grant.expires_at
} | ConvertTo-Json -Compress))).TrimEnd('=').Replace('+','-').Replace('/','_')

if (-not $SkipWorker) {
    Write-Output '  启动 worker（单轮）...'
    & $python -X utf8 (Join-Path $root 'apps\agent\agentd.py') --url $Api worker-run --grant $grantBlob --workspace $root `
        --state-path (Join-Path $env:TEMP 'demo-1.0-agentd.db') --once | ForEach-Object { Write-Output "    $_" }
    $dashboard = Api 'GET' "/api/projects/$($project.id)/dashboard"
    $executed = $dashboard.tasks | Where-Object { $_.id -eq $task.id }
    Check '任务被 Agent 自动领取并执行完成' ($executed.status -eq 'APPROVED' -or $executed.status -eq 'WAITING_REVIEW') `
        "status=$($executed.status) assignee=$($executed.assignee)"
    $agentRuns = @($dashboard.runs | Where-Object { $_.agent_id -eq $agentId })
    Check '运行台账出现该 Agent' ($agentRuns.Count -gt 0) "该 Agent 的 Run 数=$($agentRuns.Count)，项目 Run 总数=$(@($dashboard.runs).Count)"
} else {
    Write-Output '  （已跳过 worker 执行）'
}

Write-Output '=== 6/7 协作正确性（界面操作后脚本回查）==='
# 前置：让浏览器里真的有东西可点——一条机器复核建立门禁，一份发给 member-001 的交接。
# 注意重新取一次看板：上面 worker 刚执行过的任务状态已变，用旧快照会挑到一个已批准的任务。
$dashboard = Api 'GET' "/api/projects/$($project.id)/dashboard"
$gateTask = $dashboard.tasks | Where-Object { $_.status -eq 'READY' } | Select-Object -First 1
if (-not $gateTask) { throw '没有可用的 READY 任务来演示门禁批准，请先应用模板包或新建任务' }
Api 'PATCH' "/api/tasks/$($gateTask.id)?status=RUNNING" | Out-Null
Api 'PATCH' "/api/tasks/$($gateTask.id)?status=WAITING_REVIEW" | Out-Null
try {
    Api 'POST' "/api/projects/$($project.id)/reviews" @{
        target_type = 'task'; target_id = $gateTask.id; verdict = 'NEEDS_REVISION'
        summary = '机器审计：需要补充消融对照'
        findings = @(@{ severity = 'minor'; code = 'demo_machine_finding'; message = '示例发现（minor，不阻断批准）' })
        reviewer = 'platform-audit'; reviewer_kind = 'system'
    } | Out-Null
} catch {
    Write-Output "  （机器复核已存在，跳过）"
}
$center = Api 'GET' "/api/projects/$($project.id)/review-center"
$pendingGates = @($center.gates | Where-Object { $_.status -ne 'PASSED' })
Check '门禁已建立（待人工批准）' ($pendingGates.Count -gt 0) "待批准 $($pendingGates.Count) 个（门禁总数 $(@($center.gates).Count)）"

$pendingHandoff = Api 'POST' "/api/projects/$($project.id)/handoffs" @{
    task_id = $gateTask.id; objective = 'Demo 1.0：示例交接（请在 /handoffs 接受）'
    receiver = 'member-001'; receiver_type = 'member'
    completed = @('示例完成项'); key_conclusions = @('示例结论'); requires_human_approval = $false
}
Check '交接已建立（待接收方确认）' ($null -ne $pendingHandoff.id)

if ($AutoApprove) {
    Write-Output '  -AutoApprove：用 API 代替人工完成 a/b（仅回归用）'
    $gate = (Api 'GET' "/api/projects/$($project.id)/review-center").gates | Where-Object { $_.status -ne 'PASSED' } | Select-Object -First 1
    Api 'POST' "/api/projects/$($project.id)/reviews" @{
        target_type = $gate.target_type; target_id = $gate.target_id; verdict = 'APPROVED'
        summary = '人工复核通过（AutoApprove）'; reviewer = 'member-001'; reviewer_kind = 'member'
    } | Out-Null
    Api 'POST' "/api/handoffs/$($pendingHandoff.id)/accept" @{ idempotency_key = "demo-accept-$($pendingHandoff.id)" } | Out-Null
    # c) 文档保存：把内容追加一行后写回，等价于界面点「保存草稿」
    # 该端点要 multipart/form-data，Windows PowerShell 5.1 的 Invoke-RestMethod 不便构造，
    # 因此交给 python 发（演示环境必然有 python）。
    $doc = $dashboard.artifacts | Where-Object { $_.artifact_type -eq 'paper_source' } | Select-Object -First 1
    if ($doc) {
        & $python -X utf8 -c @"
import json, urllib.request, uuid
api = '$Api'
art = '$($doc.id)'
text = urllib.request.urlopen(f'{api}/api/artifacts/{art}/content', timeout=20).read().decode('utf-8', 'replace')
body = text + chr(10) + '<!-- demo-1.0 touched -->'
boundary = '----demo' + uuid.uuid4().hex
payload = (
    f'--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"doc.md\"\r\n'
    f'Content-Type: text/markdown; charset=utf-8\r\n\r\n'
).encode() + body.encode('utf-8') + f'\r\n--{boundary}--\r\n'.encode()
request = urllib.request.Request(
    f'{api}/api/artifacts/{art}/content', data=payload, method='POST',
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}', 'Idempotency-Key': 'demo-save-' + art},
)
urllib.request.urlopen(request, timeout=30).read()
print('    文档内容已保存（等价于界面「保存草稿」）')
"@ | ForEach-Object { Write-Output $_ }
    }
} else {
    Write-Output '  请在浏览器完成以下三处，然后按回车继续：'
    Write-Output '    a) /review   找到门禁卡片 → 「批准」→ 确认（批准后应变为 PASSED）'
    Write-Output '    b) /handoffs 找到待确认收据 → 「接受」（应变为 ACCEPTED）'
    Write-Output '    c) /documents 选一份文档 → 编辑 → 「保存草稿」→ 刷新页面（内容应仍在）'
    Read-Host '  完成后按回车'
}

$center = Api 'GET' "/api/projects/$($project.id)/review-center"
$passedGates = @($center.gates | Where-Object { $_.status -eq 'PASSED' })
Check '存在已通过的门禁' ($passedGates.Count -gt 0) "已通过 $($passedGates.Count) 个（门禁总数 $(@($center.gates).Count)）"
$handoffAfter = (Api 'GET' "/api/projects/$($project.id)/review-center").handoffs | Where-Object { $_.id -eq $pendingHandoff.id }
Check '交接收据已接受' ($handoffAfter.receipt_status -eq 'ACCEPTED')
$events = (Api 'GET' "/api/projects/$($project.id)/dashboard").events
$saveEvents = @($events | Where-Object { $_.event_type -eq 'artifact.content_stored' })
Check '文档保存已落库（artifact.content_stored 事件）' ($saveEvents.Count -gt 0) "content_stored 事件 $($saveEvents.Count) 条"

Write-Output '=== 7/7 四处危险操作确认（界面）==='
Write-Output '  请在浏览器确认这四处都会先弹确认框（取消应不产生任何变化）：'
Write-Output '    /drive 删除文件、/ask 删除会话、/pack 落库门禁、/delivery 生成提交包'
if (-not $AutoApprove) { Read-Host '  确认后按回车' } else { Write-Output '  （-AutoApprove：跳过人工确认步骤）' }
$dashboard = Api 'GET' "/api/projects/$($project.id)/dashboard"
$bundles = @($dashboard.artifacts | Where-Object { $_.artifact_type -eq 'submission_bundle' })
Check '取消确认后未新增提交包' ($bundles.Count -eq 0) "提交包 $($bundles.Count) 个"

Write-Output ''
Write-Output '=== 演示结果汇总 ==='
$failed = @($script:checks | Where-Object { -not $_.ok })
$script:checks | ForEach-Object { Write-Output ("  {0,-6} {1}" -f $(if ($_.ok) { 'PASS' } else { 'FAIL' }), $_.name) }
Write-Output ''
if ($failed.Count -eq 0) {
    Write-Output "全部 $($script:checks.Count) 项通过：Demo 1.0 全链路可用。"
} else {
    Write-Output "$($failed.Count)/$($script:checks.Count) 项未通过，请检查上面标记 FAIL 的项。"
    exit 1
}