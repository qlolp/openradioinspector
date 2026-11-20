# Own-window UI Automation and GDI capture, run as a bounded Windows PowerShell child.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Directory, [switch]$Probe, [switch]$FreshOnly,
      [int]$ClientId, [long]$CreationTime, [string]$InstalledExe, [string]$ReplayOrigin)
$ErrorActionPreference = 'Stop'
$stage = 'load_windows_capture_api'
$client = $null
$proof = $null
function Save-Json([string]$Name, $Value) {
    $path = Join-Path $Directory ($Name + '.json')
    [IO.File]::WriteAllText($path + '.tmp', ($Value | ConvertTo-Json -Depth 4))
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}
try {
    $stage = 'interactive_desktop'
    if ([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'session_zero' }
    if (-not [Environment]::UserInteractive) { throw 'noninteractive_window_station' }
    $stage = 'load_windows_capture_api'
    Add-Type -AssemblyName System.Drawing
    Add-Type -ReferencedAssemblies System,System.Core,System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;

public static class OwnedTauriWindow {
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int L, T, R, B; }
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern IntPtr GetThreadDesktop(uint thread);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder name, int length, out int required);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll", SetLastError=true)] static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr hwnd, IntPtr hdc, uint flags);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int w, int h, uint flags);

    static string DesktopName(IntPtr desktop) {
        var name = new StringBuilder(256);
        int size;
        if (!GetUserObjectInformation(desktop, 2, name, name.Capacity * 2, out size))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return name.ToString();
    }
    public static bool HasInputDesktop() {
        var input = OpenInputDesktop(0, false, 1); // DESKTOP_READOBJECTS, no desktop pixels are requested.
        if (input == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try { return DesktopName(input) == DesktopName(GetThreadDesktop(GetCurrentThreadId())); }
        finally { CloseDesktop(input); }
    }
    public static bool Owns(IntPtr hwnd, int pid) {
        uint owner;
        return hwnd != IntPtr.Zero && GetWindowThreadProcessId(hwnd, out owner) != 0 && owner == (uint)pid;
    }
    public static void Size(IntPtr hwnd, int pid, int width, int height) {
        if (Owns(hwnd, pid)) SetWindowPos(hwnd, IntPtr.Zero, 0, 0, width, height, 0x16); // NOMOVE | NOZORDER | NOACTIVATE
    }
    static bool HasContent(Bitmap image, Rectangle region, int colors) {
        var unique = new HashSet<int>();
        int low = 255, high = 0;
        for (int y = region.Top; y < region.Bottom; y += Math.Max(1, region.Height / 45)) {
            for (int x = region.Left; x < region.Right; x += Math.Max(1, region.Width / 80)) {
                var pixel = image.GetPixel(x,y);
                unique.Add(pixel.ToArgb());
                int brightness = (pixel.R * 3 + pixel.G * 6 + pixel.B) / 10;
                low = Math.Min(low, brightness);
                high = Math.Max(high, brightness);
            }
        }
        return unique.Count >= colors && high - low >= 14;
    }
    public static int[] Save(IntPtr hwnd, int pid, string path, int[] charts) {
        if (!Owns(hwnd, pid) || !IsWindowVisible(hwnd) || IsIconic(hwnd))
            throw new InvalidOperationException("owned_window_not_visible");
        Rect rect;
        if (!GetWindowRect(hwnd, out rect)) throw new Win32Exception(Marshal.GetLastWin32Error());
        int width = rect.R - rect.L, height = rect.B - rect.T, dpi = (int)GetDpiForWindow(hwnd);
        if (width < 390 || height < 350 || width > 4096 || height > 4096 || dpi == 0)
            throw new InvalidOperationException("owned_window_geometry");
        using (var image = new Bitmap(width, height, PixelFormat.Format24bppRgb)) {
            using (var surface = Graphics.FromImage(image)) {
                var hdc = surface.GetHdc();
                try {
                    // PW_RENDERFULLCONTENT targets exactly this HWND; there is no screen-copy fallback.
                    if (!PrintWindow(hwnd, hdc, 2)) throw new InvalidOperationException("print_window_failed");
                } finally { surface.ReleaseHdc(hdc); }
            }
            if (!Owns(hwnd, pid)) throw new InvalidOperationException("owned_window_changed");
            var body = new Rectangle(12, 60, width - 24, height - 80);
            if (!HasContent(image, body, 14)) throw new InvalidOperationException("print_window_blank_client");
            for (int i = 0; i < charts.Length; i += 4) {
                var visible = Rectangle.Intersect(body, new Rectangle(charts[i] - rect.L, charts[i+1] - rect.T,
                                                                      charts[i+2], charts[i+3]));
                if (visible.Width < 80 || visible.Height < 20 || !HasContent(image, visible, 5))
                    throw new InvalidOperationException(i == 0 ? "spectrum_not_rendered" : "waterfall_not_rendered");
            }
            image.Save(path, ImageFormat.Png);
        }
        return new int[] { width, height, dpi };
    }
}
'@
    $stage = 'interactive_desktop'
    if (-not [OwnedTauriWindow]::HasInputDesktop()) { throw 'input_desktop_differs_from_test_desktop' }
    if ($Probe) {
        Save-Json 'worker' @{status='ready'}
        return
    }
    $stage = 'installed_process_identity'
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $client = [Diagnostics.Process]::GetProcessById($ClientId)
    if ($client.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId -or
        $client.MainModule.FileName -ine [IO.Path]::GetFullPath($InstalledExe) -or
        $client.StartTime.ToUniversalTime().ToFileTimeUtc() -ne $CreationTime) { throw 'installed_process_mismatch' }
    $oldDpi = [OwnedTauriWindow]::SetThreadDpiAwarenessContext([IntPtr](-4))
    function Get-OwnedWindow {
        $client.Refresh()
        if ($client.HasExited) { throw 'installed_training_process_exited' }
        $handle = $client.MainWindowHandle
        if ([OwnedTauriWindow]::Owns($handle, $ClientId) -and $client.MainWindowTitle -cin @('OpenRadioInspector', 'OpenRadioInspector · Обзор спектра')) {
            return [Windows.Automation.AutomationElement]::FromHandle($handle)
        }
    }
    $scope = [Windows.Automation.TreeScope]::Descendants
    function Find-Named($Window, [string]$Name) {
        $criterion = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty, $Name)
        $Window.FindFirst($scope, $criterion)
    }
    function Wait-Named([string]$Name, $ControlType) {
        $end = [DateTime]::UtcNow.AddSeconds(12)
        do {
            $window = Get-OwnedWindow
            if ($window) {
                $criteria = [Windows.Automation.AndCondition]::new(
                    [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty, $Name),
                    [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty, $ControlType))
                $element = $window.FindFirst($scope, $criteria)
                if ($element -and $element.Current.IsEnabled) { return $element }
            }
            Start-Sleep -Milliseconds 200
        } until ([DateTime]::UtcNow -ge $end)
        throw 'uia_control_timeout'
    }
    function Save-Frame([string]$Scenario, [int[]]$ChartBounds, [int]$Rows) {
        $window = Get-OwnedWindow
        if (-not $window) { throw 'installed_training_window_missing' }
        $path = Join-Path $Directory ($Scenario + '.png')
        $size = [OwnedTauriWindow]::Save($client.MainWindowHandle, $ClientId, $path, $ChartBounds)
        $record = @{width=$size[0]; height=$size[1]; dpi=$size[2]; method='PrintWindow(PW_RENDERFULLCONTENT)';
                    target='installed_tauri_main_hwnd'; window_title=$client.MainWindowTitle}
        if ($Rows) { $record.waterfall_committed_rows = $Rows }
        Save-Json $Scenario $record
    }
    $stage = 'fresh_install_connection'
    $type = [Windows.Automation.ControlType]
    $entry = Wait-Named 'Адрес узла ядра' $type::Edit
    $value = $entry.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
    if ($value.Current.Value -cne 'https://radio.local:8770') { throw 'unexpected_webview_profile' }
    $null = Wait-Named 'Подключить локальный узел' $type::Button
    [OwnedTauriWindow]::Size($client.MainWindowHandle, $ClientId, 1366, 768)
    try { Save-Frame 'fresh_install_connection' @() 0 } catch {
        if ($FreshOnly) { throw }
        # A first paint may still be blank. The monitoring frame is verified independently.
    }
    if ($FreshOnly) {
        $proof = @{status='unavailable'; reason='fresh_install_only'}
        return
    }
    $stage = 'connect_private_replay'
    if ($ReplayOrigin -cnotmatch '^http://127\.0\.0\.1:[1-9][0-9]{0,4}$') { throw 'invalid_replay_origin' }
    $entry.SetFocus()
    $value.SetValue($ReplayOrigin)
    # WebView2 publishes the accessibility tree asynchronously after React updates the input.
    $editWait = [Diagnostics.Stopwatch]::StartNew()
    do {
        $entry = Wait-Named 'Адрес узла ядра' $type::Edit
        $readback = $entry.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value
        if ($readback -ceq $ReplayOrigin) { break }
        Start-Sleep -Milliseconds 150
    } while ($editWait.Elapsed.TotalSeconds -lt 5)
    if ($readback -cne $ReplayOrigin) { throw 'fixture_address_not_set' }
    (Wait-Named 'Подключиться к узлу' $type::Button).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
    $stage = 'confirm_fixture_loopback'
    (Wait-Named 'Я выбираю совместимость с локальным ядром HTTP' $type::CheckBox).GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    (Wait-Named 'Продолжить с локальным ядром HTTP' $type::Button).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
    $stage = 'monitoring_workspace'
    $null = Wait-Named 'Радиомониторинг' $type::Button
    $criterion = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty, $type::Image)
    $end = [DateTime]::UtcNow.AddSeconds(20)
    $enlarged = $false
    $stage = 'spectrum_waterfall_accessibility'
    do {
        $window = Get-OwnedWindow
        if (-not $window) { throw 'installed_training_window_missing' }
        $spectrum = $null
        $waterfall = $null
        $rows = 0
        if (Find-Named $window 'Симулятор приёмного стенда') {
            foreach ($el in $window.FindAll($scope, $criterion)) {
                $name = $el.Current.Name
                if ($name -cmatch '^Спектр: пик .+ dBm на [0-9]+ Гц$') { $spectrum = $el }
                if ($name -cmatch '^Водопад: сохранённых строк ([1-9][0-9]*), новые сверху$') {
                    $waterfall = $el
                    $rows = [int]$Matches[1]
                }
            }
        }
        if ($spectrum -and $waterfall -and $rows -ge 2) {
            $bounds = foreach ($el in @($spectrum, $waterfall)) {
                $rect = $el.Current.BoundingRectangle
                [int]$rect.X; [int]$rect.Y; [int]$rect.Width; [int]$rect.Height
            }
            $stage = 'monitoring_window_pixels'
            try {
                Save-Frame 'monitoring' $bounds $rows
                $proof = @{status='captured'}
                break
            } catch { $pixelError = $_ }
        }
        if (-not $enlarged -and [DateTime]::UtcNow -ge $end.AddSeconds(-12)) {
            [OwnedTauriWindow]::Size($client.MainWindowHandle, $ClientId, 1920, 1080)
            $enlarged = $true
        }
        Start-Sleep -Milliseconds 250
    } until ([DateTime]::UtcNow -ge $end)
    if (-not $proof) {
        if ($pixelError) { throw $pixelError }
        throw 'fixture_charts_not_visible'
    }
} catch {
    $detail = $_.Exception.GetBaseException()
    $reason = $stage + '_' + $detail.GetType().Name
    if ($detail -is [ComponentModel.Win32Exception]) { $reason = $stage + '_win32_' + $detail.NativeErrorCode }
    elseif ($detail.Message -cmatch '^[a-z][a-z0-9_]{2,79}$') { $reason = $stage + '_' + $detail.Message }
    $proof = @{status='unavailable'; reason=$reason}
} finally {
    if ($proof) { Save-Json 'worker' $proof }
    if ($oldDpi) { $null = [OwnedTauriWindow]::SetThreadDpiAwarenessContext($oldDpi) }
    if ($client) {
        # Only the verified process and HWND may receive the close request.
        if (-not $client.HasExited -and $client.StartTime.ToUniversalTime().ToFileTimeUtc() -eq $CreationTime -and
            $client.MainModule.FileName -ieq $InstalledExe -and [OwnedTauriWindow]::Owns($client.MainWindowHandle, $ClientId)) {
            $null = $client.CloseMainWindow()
        }
        $client.Dispose()
    }
}
