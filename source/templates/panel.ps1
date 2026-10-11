<#
Friendswine 控制面板 1.3.0 —— 模组对用户可见的唯一入口。

只保留三个按钮：启动、停止、错误修复。
后端动作仍由 launch.ps1（动作派发）与 app\friendswine.py 实现；本脚本只做呈现
与调用，不直接读写游戏内存，也不修改游戏文件。

正常使用：双击 启动面板.cmd
界面回归：powershell -NoProfile -ExecutionPolicy Bypass -File panel.ps1 -Preview 预览.png
          离屏把窗口渲染成 PNG，用于无人值守的界面检查；不显示窗口、不轮询状态。
#>
param(
    [string]$Action = '',
    [string]$Preview = ''
)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + [IO.Path]::PathSeparator + $env:PSModulePath
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$launch = Join-Path $root 'launch.ps1'
$python = Join-Path $root 'runtime\python.exe'
$launcher = Join-Path $root 'app\friendswine.py'
$uiTemp = Join-Path $root 'data\_ui'
$VERSION = '1.3.0'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# WinForms 原生 Button 画不出圆角，这里用一个自绘控件画「透明底 + 圆角描边」。
# 用 C# 实现是为了得到抗锯齿的圆角；编译失败时自动退回普通矩形按钮，面板照常可用。
$roundedButtonSource = @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

public class FwRoundedButton : Button
{
    private bool hovered;
    private bool pressed;

    public int Radius { get; set; }
    public Color ClearColor { get; set; }
    public Color LineColor { get; set; }
    public Color LineHoverColor { get; set; }
    public Color HoverFill { get; set; }
    public Color PressedFill { get; set; }
    public Color DisabledLine { get; set; }
    public Color DisabledText { get; set; }

    public FwRoundedButton()
    {
        Radius = 8;
        ClearColor = Color.White;
        LineColor = Color.FromArgb(203, 201, 192);
        LineHoverColor = Color.FromArgb(126, 124, 116);
        HoverFill = Color.FromArgb(247, 246, 242);
        PressedFill = Color.FromArgb(233, 232, 227);
        DisabledLine = Color.FromArgb(228, 227, 222);
        DisabledText = Color.FromArgb(163, 162, 156);
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        BackColor = Color.Transparent;
        UseVisualStyleBackColor = false;
        Cursor = Cursors.Hand;
        Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
    }

    private GraphicsPath BuildPath(Rectangle bounds)
    {
        GraphicsPath path = new GraphicsPath();
        int d = Radius * 2;
        if (d <= 0 || d > bounds.Width || d > bounds.Height)
        {
            path.AddRectangle(bounds);
            path.CloseFigure();
            return path;
        }
        path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    protected override void OnMouseEnter(EventArgs e) { hovered = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hovered = false; pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { pressed = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }
    protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        Graphics g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        g.Clear(ClearColor);
        Rectangle bounds = new Rectangle(0, 0, Width - 1, Height - 1);
        using (GraphicsPath path = BuildPath(bounds))
        {
            Color fill = Color.Empty;
            if (Enabled && pressed) fill = PressedFill;
            else if (Enabled && hovered) fill = HoverFill;
            if (fill != Color.Empty)
            {
                using (SolidBrush brush = new SolidBrush(fill)) g.FillPath(brush, path);
            }
            Color line = Enabled ? ((hovered || pressed) ? LineHoverColor : LineColor) : DisabledLine;
            using (Pen pen = new Pen(line, 1f)) g.DrawPath(pen, path);
        }
        TextRenderer.DrawText(g, Text, Font, bounds,
            Enabled ? ForeColor : DisabledText,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
    }
}
'@
$script:roundedButtonReady = $true
try {
    Add-Type -TypeDefinition $roundedButtonSource -ReferencedAssemblies @('System.Drawing', 'System.Windows.Forms') -ErrorAction Stop
} catch {
    $script:roundedButtonReady = $false
}

trap {
    $message = [string]$_.Exception.Message
    try { [void][System.Windows.Forms.MessageBox]::Show($message, 'Friendswine 面板出错', 'OK', 'Error') } catch { }
    exit 1
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function ConvertTo-ArgumentLine([string[]]$items) {
    $quoted = foreach ($item in $items) {
        if ($item -match '[\s"]') { '"' + ($item -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
        else { $item }
    }
    return ($quoted -join ' ')
}

function Start-Captured([string]$file, [string[]]$arguments, [string]$encoding) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $file
    $startInfo.Arguments = ConvertTo-ArgumentLine $arguments
    $startInfo.WorkingDirectory = $root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $textEncoding = [System.Text.Encoding]::UTF8
    if ($encoding -eq 'ansi') { $textEncoding = [System.Text.Encoding]::Default }
    $startInfo.StandardOutputEncoding = $textEncoding
    $startInfo.StandardErrorEncoding = $textEncoding
    $process = [System.Diagnostics.Process]::Start($startInfo)
    return [pscustomobject]@{
        Process    = $process
        StdOut     = $process.StandardOutput.ReadToEndAsync()
        StdErr     = $process.StandardError.ReadToEndAsync()
        Label      = ''
        ResultFile = ''
    }
}

# 三个按钮背后的动作名；全部经 launch.ps1 派发，保持单一动作定义。
$ACTION_TEXT = @{
    'start'       = '启动'
    'stop'        = '停止'
    'cache-clear' = '清理兼容缓存'
    'doctor'      = '重新诊断'
    'cleanup'     = '清理残留状态'
}
$ACTION_ADMIN = @{
    'start'       = $true
    'stop'        = $true
    'cache-clear' = $false
    'doctor'      = $false
    'cleanup'     = $false
}

# 错误修复 = 停止并恢复 → 清理残留状态 → 清理兼容缓存 → 重新诊断
$script:fixSteps = @('stop', 'cleanup', 'cache-clear', 'doctor')
$script:fixIndex = -1
$script:fixReport = $null

$clrBack = [System.Drawing.Color]::FromArgb(246, 246, 244)
$clrCard = [System.Drawing.Color]::White
$clrText = [System.Drawing.Color]::FromArgb(44, 44, 42)
$clrMuted = [System.Drawing.Color]::FromArgb(95, 94, 90)
$clrPrimary = [System.Drawing.Color]::FromArgb(15, 110, 86)
$clrPrimarySoft = [System.Drawing.Color]::FromArgb(225, 245, 238)
$clrPrimaryText = [System.Drawing.Color]::FromArgb(8, 80, 65)
$clrStop = [System.Drawing.Color]::FromArgb(163, 45, 45)
$clrDangerSoft = [System.Drawing.Color]::FromArgb(252, 235, 235)
$clrDangerText = [System.Drawing.Color]::FromArgb(121, 31, 31)
$clrNeutral = [System.Drawing.Color]::FromArgb(232, 232, 228)
$clrWarn = [System.Drawing.Color]::FromArgb(250, 238, 218)
$clrWarnText = [System.Drawing.Color]::FromArgb(133, 79, 11)

function New-PanelButton([string]$text) {
    if ($script:roundedButtonReady) {
        $button = New-Object -TypeName 'FwRoundedButton'
        $button.ClearColor = $clrCard
    } else {
        $button = New-Object System.Windows.Forms.Button
        $button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
        $button.FlatAppearance.BorderSize = 1
        $button.FlatAppearance.BorderColor = [System.Drawing.Color]::FromArgb(203, 201, 192)
        $button.BackColor = $clrCard
        $button.UseVisualStyleBackColor = $false
        $button.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
    }
    $button.Text = $text
    $button.Dock = [System.Windows.Forms.DockStyle]::Fill
    $button.ForeColor = $clrText
    $button.Margin = New-Object System.Windows.Forms.Padding(3)
    return $button
}

function New-Card([string]$title) {
    $group = New-Object System.Windows.Forms.GroupBox
    $group.Text = ' ' + $title + ' '
    $group.Dock = [System.Windows.Forms.DockStyle]::Fill
    $group.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $group.BackColor = $clrCard
    $group.ForeColor = $clrMuted
    $group.Margin = New-Object System.Windows.Forms.Padding(8, 4, 8, 4)
    return $group
}

function New-Field([string]$caption) {
    $panel = New-Object System.Windows.Forms.TableLayoutPanel
    $panel.Dock = [System.Windows.Forms.DockStyle]::Fill
    $panel.ColumnCount = 2
    $panel.RowCount = 1
    $panel.Margin = New-Object System.Windows.Forms.Padding(2, 0, 2, 0)
    [void]$panel.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 66)))
    [void]$panel.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $caption
    $label.ForeColor = $clrMuted
    $label.Dock = [System.Windows.Forms.DockStyle]::Fill
    $label.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
    $value = New-Object System.Windows.Forms.Label
    $value.Text = '—'
    $value.ForeColor = $clrText
    $value.Dock = [System.Windows.Forms.DockStyle]::Fill
    $value.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
    $value.AutoEllipsis = $true
    [void]$panel.Controls.Add($label, 0, 0)
    [void]$panel.Controls.Add($value, 1, 0)
    return [pscustomobject]@{ Panel = $panel; Value = $value }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Friendswine Q弹模组 · 控制面板 ' + $VERSION
$form.ClientSize = New-Object System.Drawing.Size(660, 620)
$form.MinimumSize = New-Object System.Drawing.Size(660, 560)
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.BackColor = $clrBack
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$form.ShowInTaskbar = $true

$grid = New-Object System.Windows.Forms.TableLayoutPanel
$grid.Dock = [System.Windows.Forms.DockStyle]::Fill
$grid.ColumnCount = 1
$grid.RowCount = 4
$grid.Padding = New-Object System.Windows.Forms.Padding(6, 6, 6, 0)
[void]$grid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
foreach ($height in @(60, 140, 108)) {
    [void]$grid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, $height)))
}
[void]$grid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))

$header = New-Object System.Windows.Forms.TableLayoutPanel
$header.Dock = [System.Windows.Forms.DockStyle]::Fill
$header.ColumnCount = 2
$header.RowCount = 1
$header.Margin = New-Object System.Windows.Forms.Padding(2, 0, 2, 0)
[void]$header.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
[void]$header.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 260)))
$titles = New-Object System.Windows.Forms.Panel
$titles.Dock = [System.Windows.Forms.DockStyle]::Fill
$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = 'Friendswine Q弹模组'
$titleLabel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 14)
$titleLabel.ForeColor = $clrText
$titleLabel.AutoSize = $true
$titleLabel.Location = New-Object System.Drawing.Point(4, 8)
$subLabel = New-Object System.Windows.Forms.Label
$subLabel.Text = '只有三个按钮：启动、停止；有问题就点「错误修复」'
$subLabel.ForeColor = $clrMuted
$subLabel.AutoSize = $true
$subLabel.Location = New-Object System.Drawing.Point(6, 38)
[void]$titles.Controls.Add($titleLabel)
[void]$titles.Controls.Add($subLabel)
$badges = New-Object System.Windows.Forms.FlowLayoutPanel
$badges.Dock = [System.Windows.Forms.DockStyle]::Fill
$badges.FlowDirection = [System.Windows.Forms.FlowDirection]::RightToLeft
$badges.WrapContents = $false
$badges.Padding = New-Object System.Windows.Forms.Padding(0, 12, 4, 0)
$phaseBadge = New-Object System.Windows.Forms.Label
$phaseBadge.Text = ' 读取状态… '
$phaseBadge.AutoSize = $true
$phaseBadge.Padding = New-Object System.Windows.Forms.Padding(10, 5, 10, 5)
$phaseBadge.BackColor = $clrNeutral
$phaseBadge.ForeColor = $clrMuted
$permBadge = New-Object System.Windows.Forms.Label
$permBadge.Text = ' 普通权限 '
$permBadge.AutoSize = $true
$permBadge.Padding = New-Object System.Windows.Forms.Padding(10, 5, 10, 5)
$permBadge.BackColor = $clrWarn
$permBadge.ForeColor = $clrWarnText
$permBadge.Margin = New-Object System.Windows.Forms.Padding(0, 0, 8, 0)
if ($elevated) {
    $permBadge.Text = ' 管理员 '
    $permBadge.BackColor = $clrPrimary
    $permBadge.ForeColor = [System.Drawing.Color]::White
}
[void]$badges.Controls.Add($permBadge)
[void]$badges.Controls.Add($phaseBadge)
[void]$header.Controls.Add($titles, 0, 0)
[void]$header.Controls.Add($badges, 1, 0)
[void]$grid.Controls.Add($header, 0, 0)

$statusCard = New-Card '当前状态'
$statusGrid = New-Object System.Windows.Forms.TableLayoutPanel
$statusGrid.Dock = [System.Windows.Forms.DockStyle]::Fill
$statusGrid.ColumnCount = 4
$statusGrid.RowCount = 4
$statusGrid.Padding = New-Object System.Windows.Forms.Padding(8, 4, 8, 4)
[void]$statusGrid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 66)))
[void]$statusGrid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 50)))
[void]$statusGrid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 66)))
[void]$statusGrid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 50)))
foreach ($unused in 1..3) {
    [void]$statusGrid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 26)))
}
[void]$statusGrid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$fPhase = New-Field '阶段'
$fTargets = New-Field '目标数'
$fCompat = New-Field '兼容校验'
$fData = New-Field '数据目录'
[void]$statusGrid.Controls.Add($fPhase.Panel, 0, 0)
[void]$statusGrid.Controls.Add($fTargets.Panel, 2, 0)
[void]$statusGrid.Controls.Add($fCompat.Panel, 0, 1)
[void]$statusGrid.Controls.Add($fData.Panel, 0, 2)
$statusGrid.SetColumnSpan($fPhase.Panel, 2)
$statusGrid.SetColumnSpan($fTargets.Panel, 2)
$statusGrid.SetColumnSpan($fCompat.Panel, 4)
$statusGrid.SetColumnSpan($fData.Panel, 4)
$verdictLabel = New-Object System.Windows.Forms.Label
$verdictLabel.Text = '正在读取当前状态…'
$verdictLabel.ForeColor = $clrText
$verdictLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$verdictLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$verdictLabel.AutoEllipsis = $true
$statusGrid.SetColumnSpan($verdictLabel, 4)
[void]$statusGrid.Controls.Add($verdictLabel, 0, 3)
[void]$statusCard.Controls.Add($statusGrid)
[void]$grid.Controls.Add($statusCard, 0, 1)

$actionCard = New-Card '操作'
$actionGrid = New-Object System.Windows.Forms.TableLayoutPanel
$actionGrid.Dock = [System.Windows.Forms.DockStyle]::Fill
$actionGrid.ColumnCount = 3
$actionGrid.RowCount = 2
$actionGrid.Padding = New-Object System.Windows.Forms.Padding(8, 4, 8, 4)
[void]$actionGrid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 46)))
[void]$actionGrid.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
foreach ($unused in 1..3) {
    [void]$actionGrid.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 33.33)))
}
$btnStart = New-PanelButton '启动'
$btnStop = New-PanelButton '停止'
$btnFix = New-PanelButton '错误修复'
[void]$actionGrid.Controls.Add($btnStart, 0, 0)
[void]$actionGrid.Controls.Add($btnStop, 1, 0)
[void]$actionGrid.Controls.Add($btnFix, 2, 0)
$actionHint = New-Object System.Windows.Forms.Label
$actionHint.Text = '「启动」在角色时装展示页与匹配对局都能生效；退出游戏前请点「停止」恢复模型。'
$actionHint.ForeColor = $clrMuted
$actionHint.Dock = [System.Windows.Forms.DockStyle]::Fill
$actionHint.TextAlign = [System.Drawing.ContentAlignment]::MiddleLeft
$actionHint.Padding = New-Object System.Windows.Forms.Padding(6, 0, 0, 0)
$actionHint.AutoEllipsis = $true
$actionGrid.SetColumnSpan($actionHint, 3)
[void]$actionGrid.Controls.Add($actionHint, 0, 1)
[void]$actionCard.Controls.Add($actionGrid)
[void]$grid.Controls.Add($actionCard, 0, 2)

$logCard = New-Card '运行输出'
$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ReadOnly = $true
$logBox.ScrollBars = [System.Windows.Forms.ScrollBars]::Vertical
$logBox.Dock = [System.Windows.Forms.DockStyle]::Fill
$logBox.BackColor = [System.Drawing.Color]::FromArgb(252, 252, 250)
$logBox.ForeColor = $clrText
$logBox.Font = New-Object System.Drawing.Font('Consolas', 9)
$logBox.BorderStyle = [System.Windows.Forms.BorderStyle]::FixedSingle
$logBox.WordWrap = $true
$logBox.Text = '面板已就绪。这里会显示每一步动作的原始输出。'
[void]$logCard.Controls.Add($logBox)
[void]$grid.Controls.Add($logCard, 0, 3)

$statusStrip = New-Object System.Windows.Forms.StatusStrip
$statusStrip.BackColor = $clrBack
$statusInfo = New-Object System.Windows.Forms.ToolStripStatusLabel
$statusInfo.Text = '就绪'
$statusStamp = New-Object System.Windows.Forms.ToolStripStatusLabel
$statusStamp.Spring = $true
$statusStamp.TextAlign = [System.Drawing.ContentAlignment]::MiddleRight
[void]$statusStrip.Items.Add($statusInfo)
[void]$statusStrip.Items.Add($statusStamp)
[void]$form.Controls.Add($grid)
[void]$form.Controls.Add($statusStrip)

$script:ui = @{ Phase = $phaseBadge; PhaseCard = $fPhase.Value; Targets = $fTargets.Value
                Compat = $fCompat.Value; Data = $fData.Value
                Verdict = $verdictLabel; Info = $statusInfo; Stamp = $statusStamp
                Log = $logBox }
$script:actionJob = $null
$script:pollJob = $null
$script:lastPoll = [DateTime]::MinValue
$script:logText = ''

function Write-Log([string]$text) {
    if (-not $text) { return }
    $script:logText = $text
    $logBox.Text = $text
    $logBox.SelectionStart = 0
    $logBox.ScrollToCaret()
}

function Split-Report([string]$text) {
    $payload = $null
    $verdict = ''
    if ($text) {
        $start = $text.IndexOf('{')
        $end = $text.LastIndexOf('}')
        if ($start -ge 0 -and $end -gt $start) {
            try { $payload = $text.Substring($start, $end - $start + 1) | ConvertFrom-Json } catch { $payload = $null }
            $verdict = $text.Substring($end + 1).Trim()
        } else {
            $verdict = $text.Trim()
        }
    }
    return [pscustomobject]@{ Payload = $payload; Verdict = $verdict }
}

function Get-PhaseText([string]$phase) {
    switch ($phase) {
        'RUNNING'            { return '运行中' }
        'WAITING_FOR_MODELS' { return '等待模型' }
        'WAITING_FOR_SCENE'  { return '等待进入场景' }
        'PAUSED'             { return '已暂停' }
        'RESTORE_FAULT'      { return '异常' }
        'STOPPED'            { return '已停止' }
        'NOT_ATTACHED'       { return '未启动' }
    }
    if ($phase) { return $phase }
    return '未启动'
}

function Set-PhaseBadge([string]$phase) {
    $script:ui.Phase.Text = ' ' + (Get-PhaseText $phase) + ' '
    $back = $clrNeutral
    $fore = $clrMuted
    if ($phase -eq 'RUNNING' -or $phase -eq 'WAITING_FOR_MODELS') {
        $back = $clrPrimarySoft
        $fore = $clrPrimaryText
    } elseif ($phase -eq 'PAUSED') {
        $back = $clrWarn
        $fore = $clrWarnText
    } elseif ($phase -eq 'RESTORE_FAULT') {
        $back = $clrDangerSoft
        $fore = $clrDangerText
    }
    $script:ui.Phase.BackColor = $back
    $script:ui.Phase.ForeColor = $fore
}

function Update-StatusView([string]$text) {
    $report = Split-Report $text
    $payload = $report.Payload
    if (-not $payload) {
        $script:ui.Verdict.Text = if ($report.Verdict) { $report.Verdict } else { '状态读取失败' }
        return
    }
    $phase = [string]$payload.phase
    if (-not $phase) { $phase = 'NOT_ATTACHED' }
    Set-PhaseBadge $phase
    $script:ui.PhaseCard.Text = Get-PhaseText $phase
    $targets = $payload.targets
    if ($null -eq $targets) { $targets = '—' }
    $script:ui.Targets.Text = [string]$targets
    if ($payload.pid) { $script:ui.Targets.Text = [string]$targets + '（PID ' + [string]$payload.pid + '）' }
    $compat = $payload.compatibility
    if ($compat) { $script:ui.Compat.Text = [string]$compat }
    else { $script:ui.Compat.Text = '尚未在本局校验' }
    if ($payload.state_dir) { $script:ui.Data.Text = [string]$payload.state_dir }
    if ($report.Verdict) {
        $script:ui.Verdict.Text = $report.Verdict
        $script:ui.Verdict.ForeColor = $clrText
        if ($report.Verdict.StartsWith('发现问题')) { $script:ui.Verdict.ForeColor = $clrStop }
    } else {
        $script:ui.Verdict.Text = '—'
    }
    $script:ui.Stamp.Text = '状态更新于 ' + (Get-Date).ToString('HH:mm:ss')
}

function Set-Busy([bool]$busy, [string]$message) {
    foreach ($button in @($btnStart, $btnStop, $btnFix)) { $button.Enabled = -not $busy }
    $script:ui.Info.Text = if ($busy) { $message } else { '就绪' }
    if ($busy) { $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor }
    else { $form.Cursor = [System.Windows.Forms.Cursors]::Default }
}

function Restart-Elevated([string]$pendingAction) {
    $panelPath = (Join-Path $root 'panel.ps1').Replace("'", "''")
    $command = "& '$panelPath'"
    if ($pendingAction) { $command += " -Action '$pendingAction'" }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden `
        -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) | Out-Null
    $form.Close()
}

function Start-Action([string]$key, [string]$label, [bool]$checkAdmin) {
    $text = if ($label) { $label } else { $ACTION_TEXT[$key] }
    if ($checkAdmin -and $ACTION_ADMIN[$key] -and -not $elevated) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            '「' + $text + '」需要管理员权限。将重新以管理员身份打开面板（只需这一次确认）。',
            '需要管理员权限', 'OKCancel', 'Information')
        if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { Restart-Elevated $key }
        return $false
    }
    if (-not (Test-Path -LiteralPath $uiTemp)) { [void][IO.Directory]::CreateDirectory($uiTemp) }
    $resultFile = Join-Path $uiTemp ('action-' + [Guid]::NewGuid().ToString('N') + '.txt')
    $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $launch,
                   '-Action', $key, '-ResultFile', $resultFile)
    # 「启动」必须带上 -Training（=> --battle）：这是已审核的对局适配。
    # 漏掉它就只启用时装展示能力，在对局里会一直停在 WAITING_FOR_SCENE。
    if ($key -eq 'start') { $arguments += '-Training' }
    Set-Busy $true ('正在' + $text + '，请勿关闭窗口…')
    $job = Start-Captured 'powershell.exe' $arguments 'ansi'
    $job.Label = $text
    $job.ResultFile = $resultFile
    $script:actionJob = $job
    return $true
}

function Start-StatusPoll {
    if (-not (Test-Path -LiteralPath $python)) { return }
    $script:pollJob = Start-Captured $python @('-B', '-X', 'utf8', $launcher, 'status') 'utf8'
}

function Start-FixStep {
    if ($script:fixIndex -ge $script:fixSteps.Count) {
        $lines = @('修复流程完成：已停止并恢复、清理残留状态、清理兼容缓存、重新诊断。')
        if ($script:fixReport) { $lines += $script:fixReport.TrimEnd() }
        $lines += ''
        $lines += $script:logText
        $script:fixIndex = -1
        $script:fixReport = $null
        Write-Log (($lines -join "`r`n").Trim())
        Set-Busy $false ''
        $script:lastPoll = [DateTime]::MinValue
        return
    }
    $key = $script:fixSteps[$script:fixIndex]
    $label = '错误修复 ' + ($script:fixIndex + 1) + '/' + $script:fixSteps.Count + '：' + $ACTION_TEXT[$key]
    if (-not (Start-Action $key $label $false)) {
        $script:fixIndex = -1
        $script:fixReport = $null
        Set-Busy $false ''
    }
}

function Complete-Action {
    $job = $script:actionJob
    $script:actionJob = $null
    $stdout = $job.StdOut.Result
    $stderr = $job.StdErr.Result
    $code = $job.Process.ExitCode
    $text = ''
    if ($job.ResultFile -and (Test-Path -LiteralPath $job.ResultFile)) {
        $text = [IO.File]::ReadAllText($job.ResultFile, [System.Text.Encoding]::UTF8)
        Remove-Item -LiteralPath $job.ResultFile -Force -ErrorAction SilentlyContinue
    }
    if (-not $text.Trim()) { $text = ($stdout + "`r`n" + $stderr).Trim() }
    $report = Split-Report $text
    $block = @()
    $block += '【' + $job.Label + '】退出码 ' + [string]$code + ' · ' + (Get-Date).ToString('HH:mm:ss')
    if ($report.Verdict) { $block += $report.Verdict }
    $block += ''
    $block += $text.Trim()
    if ($script:logText) {
        Write-Log ($script:logText.TrimEnd() + "`r`n`r`n" + ($block -join "`r`n"))
    } else {
        Write-Log ($block -join "`r`n")
    }
    if ($script:fixIndex -ge 0) {
        $script:fixReport += ('· ' + $job.Label + ' → 退出码 ' + [string]$code + "`r`n")
        $script:fixIndex++
        Start-FixStep
        return
    }
    Set-Busy $false ''
    $script:lastPoll = [DateTime]::MinValue
}

function Complete-Poll {
    $job = $script:pollJob
    $script:pollJob = $null
    $text = $job.StdOut.Result + "`r`n" + $job.StdErr.Result
    if ($text.Trim()) { Update-StatusView $text }
    $script:lastPoll = [DateTime]::Now
}

function Start-Fix {
    if ($script:actionJob) { return }
    $answer = [System.Windows.Forms.MessageBox]::Show(
        ('错误修复会依次执行：停止并恢复 → 清理兼容缓存 → 重新诊断。' +
         "`r`n`r`n如果模组正在运行，会先停止并恢复模型。确定继续？"), '错误修复', 'OKCancel', 'Question')
    if ($answer -ne [System.Windows.Forms.DialogResult]::OK) { return }
    if (-not $elevated) {
        $reply = [System.Windows.Forms.MessageBox]::Show(
            '错误修复需要管理员权限。将重新以管理员身份打开面板（只需这一次确认）。',
            '需要管理员权限', 'OKCancel', 'Information')
        if ($reply -eq [System.Windows.Forms.DialogResult]::OK) { Restart-Elevated 'fix' }
        return
    }
    $script:fixReport = ''
    $script:fixIndex = 0
    $script:logText = ''
    Start-FixStep
}

$btnStart.Add_Click({
    if ($script:actionJob) { return }
    [void](Start-Action 'start' '' $true)
})
$btnStop.Add_Click({
    if ($script:actionJob) { return }
    $answer = [System.Windows.Forms.MessageBox]::Show('确定停止效果并恢复模型？', '停止', 'OKCancel', 'Question')
    if ($answer -eq [System.Windows.Forms.DialogResult]::OK) { [void](Start-Action 'stop' '' $true) }
})
$btnFix.Add_Click({ Start-Fix })

$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 250
$script:timer.Add_Tick({
    if ($script:actionJob) {
        if ($script:actionJob.Process.HasExited) { Complete-Action }
        return
    }
    if ($script:pollJob) {
        if ($script:pollJob.Process.HasExited) { Complete-Poll }
        return
    }
    $interval = 3000
    if (-not $form.ContainsFocus) { $interval = 9000 }
    if (([DateTime]::Now - $script:lastPoll).TotalMilliseconds -ge $interval) {
        $script:lastPoll = [DateTime]::Now
        Start-StatusPoll
    }
})
$form.Add_Shown({
    if ($Preview) { return }
    $script:timer.Start()
    if ($Action -eq 'fix') { Start-Fix }
    elseif ($Action -eq 'start' -or $Action -eq 'stop') { [void](Start-Action $Action '' $true) }
})
$form.Add_FormClosing({
    $script:timer.Stop()
    if ($script:actionJob) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            '动作仍在执行中。关闭面板不会取消它，游戏内的效果也不会停止。确定关闭？',
            '动作正在执行', 'OKCancel', 'Warning')
        if ($answer -ne [System.Windows.Forms.DialogResult]::OK) {
            $_.Cancel = $true
            $script:timer.Start()
            return
        }
        try { $script:actionJob.Process.Kill() } catch { }
    }
})

if ($Preview) {
    Set-PhaseBadge 'RUNNING'
    $script:ui.PhaseCard.Text = '运行中'
    $script:ui.Targets.Text = '5（PID 40260）'
    $script:ui.Compat.Text = '展示与对局均已通过只读检查'
    $script:ui.Data.Text = Join-Path $root 'data\Friendswine\501a0013069fe0fe'
    $script:ui.Verdict.Text = '诊断结果正常：模组正在运行'
    $script:ui.Stamp.Text = '状态更新于 22:41:12'
    Write-Log "【启动】退出码 0 · 22:40:58`r`n诊断结果正常：展示与对局所需的脚本接口均已通过只读检查`r`n`r`n{`r`n  `"started`": true,`r`n  `"profile_id`": `"neox-20261009-py311`",`r`n  `"phase`": `"RUNNING`",`r`n  `"targets`": 5`r`n}"
    $form.ShowInTaskbar = $false
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $form.Location = New-Object System.Drawing.Point(-6000, -6000)
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.Application]::DoEvents()
    $form.Refresh()
    [System.Windows.Forms.Application]::DoEvents()
    $bitmap = New-Object System.Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($bitmap, (New-Object System.Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $bitmap.Save($Preview, [System.Drawing.Imaging.ImageFormat]::Png)
    $bitmap.Dispose()
    $form.Close()
    Write-Output ('preview written: ' + $Preview)
    return
}

[void]$form.ShowDialog()
