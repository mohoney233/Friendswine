param(
    [ValidateSet('start','start-watch','pause','resume','stop','status','doctor','check-update','validate','profiles','profile-import','cache-clear','cleanup')]
    [string]$Action = 'start',
    [switch]$Training,
    [string]$ResultFile = '',
    [string]$ProfileFile = ''
)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + $env:PSModulePath
$root = $PSScriptRoot
$python = Join-Path $root 'runtime\python.exe'
$launcher = Join-Path $root 'app\friendswine.py'
# Progress text lives here (PowerShell) rather than in the batch entry point:
# cmd.exe mis-reads UTF-8 batch text once the console code page is not already
# UTF-8, while PowerShell always encodes output to the current console. The
# control panel passes -ResultFile, which suppresses these banners and hands the
# merged output back as a UTF-8 file instead of console text.
$interactive = -not $ResultFile
if ($interactive) {
    $banners = @{
        'start' = '正在启动效果，请先进入角色时装展示页或匹配对局...'
        'start-watch' = '正在开启自动重连，游戏重启后会自动生效...'
        'pause' = '正在暂停效果并恢复当前形状...'
        'resume' = '正在继续效果...'
        'stop' = '正在停止效果并恢复模型，请勿关闭游戏...'
        'status' = '当前状态如下...'
        'doctor' = '正在做只读环境诊断，不会修改游戏...'
        'check-update' = '正在检查游戏更新兼容性，不会修改游戏...'
        'validate' = '正在做兼容性验证，只做只读检查，不会启动缩放...'
        'profiles' = '当前兼容配置如下...'
        'profile-import' = '导入适配包，请按提示输入本地 JSON/ZIP 文件路径...'
        'cache-clear' = '正在清理兼容缓存...'
        'cleanup' = '正在归档并清除上一次会话的残留状态，不会修改游戏...'
    }
    if ($banners.ContainsKey($Action)) { Write-Output $banners[$Action] }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (($Action -in @('start','start-watch','pause','resume','stop','validate')) -and -not $admin) {
    if ($ResultFile) { throw '未能获得管理员权限，操作已取消。' }
    # The elevation hand-off result lives inside the installation directory so
    # the mod never writes into the system drive's %TEMP%.
    $resultPath = Join-Path $root ('.friendswine-elevate-' + [Guid]::NewGuid().ToString('N') + '.txt')
    $scriptPath = (Join-Path $root 'launch.ps1').Replace("'", "''")
    $resultLiteral = $resultPath.Replace("'", "''")
    $command = "& '$scriptPath' -Action '$Action' -ResultFile '$resultLiteral'"
    if ($Training) { $command += ' -Training' }
    if ($ProfileFile) { $command += " -ProfileFile '" + ($ProfileFile.Replace("'", "''")) + "'" }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    try {
        $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',$encoded) -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if (Test-Path -LiteralPath $resultPath) { Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 }
        exit $process.ExitCode
    } finally {
        if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force }
    }
}
# The bundled interpreter is started through the .NET process API rather than the
# call operator. Native stderr must stay a diagnostic instead of being promoted to
# a terminating PowerShell error (the old "2>&1" plus ErrorActionPreference='Stop'
# turned any message into NativeCommandError), and both streams are decoded as
# UTF-8 even when the host console uses another code page -- which is the case for
# the elevated console created by the UAC prompt.
$arguments = @('-B','-X','utf8',$launcher,$Action)
if ($Training) { $arguments += '--battle' }
if ($ProfileFile) { $arguments += @('--file', $ProfileFile) }
function ConvertTo-ArgumentLine([string[]]$items) {
    $quoted = foreach ($item in $items) {
        if ($item -match '[\s"]') { '"' + ($item -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
        else { $item }
    }
    return ($quoted -join ' ')
}
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $python
$startInfo.Arguments = ConvertTo-ArgumentLine $arguments
$startInfo.WorkingDirectory = $root
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
try {
    $process = [System.Diagnostics.Process]::Start($startInfo)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $code = $process.ExitCode
    $lines = @()
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    if ($stdout) { $lines += $stdout.TrimEnd([char]13,[char]10) }
    if ($stderr) { $lines += $stderr.TrimEnd([char]13,[char]10) }
    if ($ResultFile) {
        [IO.File]::WriteAllText($ResultFile, ($lines -join [Environment]::NewLine), (New-Object System.Text.UTF8Encoding($false)))
    } elseif ($lines.Count) {
        foreach ($line in $lines) { Write-Output $line }
    }
    exit $code
} catch {
    $message = [string]$_.Exception.Message
    if ($ResultFile) { [IO.File]::WriteAllText($ResultFile, $message, (New-Object System.Text.UTF8Encoding($false))) }
    else { [Console]::Error.WriteLine($message) }
    exit 1
}