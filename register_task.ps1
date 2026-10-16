# 注册 / 卸载 Windows 计划任务
#
# 用法（普通 PowerShell 即可，不需要管理员）：
#   .\register_task.ps1                      # 注册（工作日 09:05 + 可选登录补推）
#   .\register_task.ps1 -Time 09:10          # 自定义时间
#   .\register_task.ps1 -Unregister          # 卸载
#   .\register_task.ps1 -Status              # 查看状态
#   .\register_task.ps1 -RunNow              # 立即试跑一次
#
# 设计要点（来自需求文档与实测）：
#   1. 必须勾选「错过计划开始时间后尽快启动」(-StartWhenAvailable)
#      —— 否则 09:05 前未开机时任务永不触发且静默失败
#   2. 工作日限制用 -DaysOfWeek Monday..Friday
#   3. 登录触发器无法限制星期，因此脚本内自行跳过周末（本脚本默认不加登录触发器，
#      如需补推兜底请加 -WithLogonTrigger）
#   4. 时间默认 09:05：非整点非半点，避开飞书官方提示的 10:00/17:30 等系统压力高峰
#      （否则可能出现 11232 限流错误）

param(
    [string]$TaskName = "DailyDigestFeishu",
    [string]$Time = "09:05",
    [switch]$Unregister,
    [switch]$Status,
    [switch]$RunNow,
    [switch]$WithLogonTrigger
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $ScriptDir "daily_digest.py"

function Get-PythonPath {
    $candidates = @("python.exe", "python3.exe")
    foreach ($c in $candidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "未找到 python.exe，请先安装 Python 3.9+ 并加入 PATH"
}

function Show-Status {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "任务 '$TaskName' 未注册。" ; return }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "任务名称 : $($t.TaskName)"
    Write-Host "状态     : $($t.State)"
    Write-Host "上次运行 : $($info.LastRunTime)  结果码: $($info.LastTaskResult)"
    Write-Host "下次运行 : $($info.NextRunTime)"
    Write-Host "触发器   :"
    $t.Triggers | ForEach-Object {
        $dow = if ($_.DaysOfWeek) { $_.DaysOfWeek } else { "-" }
        Write-Host ("   [{0}] 时间={1} 星期={2} 错过则尽快={3}" -f `
            $_.CimClass.CimClassName, $_.StartBoundary, $dow, $_.StartWhenAvailable)
    }
    Write-Host "动作     : $($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" })"
}

if ($Status) { Show-Status; exit 0 }

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已卸载任务 '$TaskName'。"
    } else {
        Write-Host "任务 '$TaskName' 不存在，无需卸载。"
    }
    exit 0
}

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "已触发任务 '$TaskName'，稍后可用 -Status 查看结果。"
    exit 0
}

# ---------------- 注册 ----------------
if (-not (Test-Path $Script)) { throw "找不到主程序：$Script" }
$Python = Get-PythonPath
$Config = Join-Path $ScriptDir "config.json"
if (-not (Test-Path $Config)) {
    Write-Warning "尚未创建 config.json，请先复制 config.example.json 为 config.json 并填入飞书 webhook。"
}

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`"" -WorkingDirectory $ScriptDir

# ① 工作日定时
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

# ② 登录补推（可选兜底；脚本内自行跳过周末）
$triggers = @($trigger)
if ($WithLogonTrigger) {
    $triggers += New-ScheduledTaskTrigger -AtLogOn
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "任务已存在，先移除旧的注册。"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description "工作日 $Time 推送每日资讯日报到飞书（错过则尽快启动）。日志见 logs/digest.log" | Out-Null

Write-Host "已注册任务 '$TaskName'：工作日 $Time 触发（StartWhenAvailable=已开启）"
Write-Host "登录补推触发器：$(if ($WithLogonTrigger) { '已启用' } else { '未启用（可加 -WithLogonTrigger）' })"
Write-Host ""
Show-Status
Write-Host ""
Write-Host "提示：首次运行前请先执行  python daily_digest.py --dry-run  确认能抓满 20 条。"
