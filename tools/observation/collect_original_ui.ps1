<#
.SYNOPSIS
    Сбор свидетельств интерфейса и функциональности коммерческого RadioInspector.

.DESCRIPTION
    Скрипт работает только на чтение: он не изменяет файлы оригинала, не
    трогает реестр и не вмешивается в работу программы. Он собирает то, что
    недоступно при разборе бинарников:

    * перечень всех видимых окон с заголовками, классами, координатами и
      принадлежностью к процессам — это карта интерфейса;
    * снимки всех мониторов и отдельные снимки окон программы;
    * дерево процессов оригинала с путями, версиями и загруженными модулями;
    * перечень установленных компонентов комплекса и файлов данных;
    * содержимое журналов и конфигурационных файлов, доступные без прав админа;
    * ключи реестра, относящиеся к правообладателю (только чтение).

    Результат складывается в каталог: JSON-манифест, CSV с окнами, PNG со
    снимками и текстовый отчёт. Каталог нужно заархивировать и передать —
    содержимое снимков программы в публичный репозиторий не выкладывается.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File collect_original_ui.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File collect_original_ui.ps1 -OutDir D:\ori -ProcessFilter "Inspector,RInspector"
#>

[CmdletBinding()]
param(
    [string]$OutDir = (Join-Path $env:USERPROFILE "Desktop\ori-observation"),
    [string]$ProcessFilter = "Inspector",
    [switch]$NoScreenshots
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# --------------------------------------------------------------------------
# Подготовка каталогов
# --------------------------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$root = Join-Path $OutDir $stamp
$screenDir = Join-Path $root "screens"
$windowDir = Join-Path $root "windows"
$textDir = Join-Path $root "text"
foreach ($dir in @($root, $screenDir, $windowDir, $textDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

$report = New-Object System.Collections.Generic.List[string]
function Write-Report {
    param([string]$Line)
    $report.Add($Line)
    Write-Host $Line
}
function Save-Text {
    param([string]$Name, [string]$Content)
    $path = Join-Path $textDir $Name
    Set-Content -LiteralPath $path -Value $Content -Encoding UTF8
    return $path
}

Write-Report "Сбор наблюдения RadioInspector"
Write-Report "Каталог: $root"
Write-Report "Время: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')"
Write-Report ("Хост: {0}  Пользователь: {1}" -f $env:COMPUTERNAME, $env:USERNAME)

# --------------------------------------------------------------------------
# 1. Перечень окон
# --------------------------------------------------------------------------
Write-Report ""
Write-Report "[1] Перечень окон"

if (-not ("Win32Window" -as [type])) {
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public class Win32Window {
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hWnd, ref POINT point);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hWnd);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X, Y; }

    public class Info {
        public long Handle;
        public string Title = "";
        public string Class = "";
        public long ProcessId;
        public bool Visible;
        public int Left, Top, Width, Height;
        public long Parent;
    }

    static string Text(IntPtr hWnd) {
        var sb = new StringBuilder(1024);
        GetWindowTextW(hWnd, sb, sb.Capacity);
        return sb.ToString();
    }

    static string Cls(IntPtr hWnd) {
        var sb = new StringBuilder(256);
        GetClassNameW(hWnd, sb, sb.Capacity);
        return sb.ToString();
    }

    public static List<Info> All() {
        var list = new List<Info>();
        EnumWindows((hWnd, lParam) => {
            uint pid;
            GetWindowThreadProcessId(hWnd, out pid);
            RECT r;
            GetWindowRect(hWnd, out r);
            list.Add(new Info {
                Handle = hWnd.ToInt64(),
                Title = Text(hWnd),
                Class = Cls(hWnd),
                ProcessId = pid,
                Visible = IsWindowVisible(hWnd),
                Left = r.Left, Top = r.Top,
                Width = r.Right - r.Left, Height = r.Bottom - r.Top,
                Parent = GetParent(hWnd).ToInt64()
            });
            return true;
        }, IntPtr.Zero);
        return list;
    }
}
"@
}

$windows = @()
try {
    $windows = [Win32Window]::All()
} catch {
    Write-Report "  [!] Перечисление окон не удалось: $($_.Exception.Message)"
}

$processMap = @{}
foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
    $processMap[[int]$process.Id] = $process.ProcessName
}

$windowRows = foreach ($w in $windows) {
    $processName = $processMap[[int]$w.ProcessId]
    [pscustomobject]@{
        handle = $w.Handle
        title = $w.Title
        class = $w.Class
        process_id = $w.ProcessId
        process = $processName
        visible = $w.Visible
        left = $w.Left
        top = $w.Top
        width = $w.Width
        height = $w.Height
        parent = $w.Parent
        belongs_to_original = [bool]($processName -and $processName -match $ProcessFilter)
    }
}
$windowRows | Export-Csv -LiteralPath (Join-Path $root "windows.csv") -NoTypeInformation -Encoding UTF8
$windowRows | Where-Object { $_.visible -and $_.width -gt 0 } |
    Format-Table handle, process, class, width, height, title -AutoSize |
    Out-String -Width 400 | Set-Content -LiteralPath (Join-Path $textDir "windows-visible.txt") -Encoding UTF8
$originalWindows = @($windowRows | Where-Object { $_.belongs_to_original -and $_.visible })
Write-Report "  Всего окон: $($windows.Count); видимых окон оригинала: $($originalWindows.Count)"

# --------------------------------------------------------------------------
# 2. Процессы оригинала
# --------------------------------------------------------------------------
Write-Report ""
Write-Report "[2] Процессы оригинала"

$pattern = $ProcessFilter -replace ",", "|"
$processInfo = New-Object System.Collections.Generic.List[string]
foreach ($process in Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match $pattern }) {
    $line = "=== $($process.ProcessName) (PID $($process.Id))"
    try {
        $line += "  path=$($process.Path)"
        $file = Get-Item -LiteralPath $process.Path -ErrorAction SilentlyContinue
        if ($file) {
            $line += "  version=$($file.VersionInfo.FileVersion)  product=$($file.VersionInfo.ProductName)  company=$($file.VersionInfo.CompanyName)"
        }
        $line += "  started=$($process.StartTime)  title=$($process.MainWindowTitle)"
    } catch {
        $line += "  [сведения недоступны: $($_.Exception.Message)]"
    }
    $processInfo.Add($line)
    try {
        $modules = $process.Modules | ForEach-Object { "    $($_.ModuleName)  $($_.FileName)" }
        if ($modules) {
            $processInfo.Add(($modules -join "`n"))
        }
    } catch {
        $processInfo.Add("    [список модулей недоступен: $($_.Exception.Message)]")
    }
}
if ($processInfo.Count -eq 0) {
    $processInfo.Add("Процессы, совпадающие с фильтром '$ProcessFilter', не найдены.")
}
Save-Text -Name "processes.txt" -Content ($processInfo -join "`n")
Write-Report "  Записано сведений о процессах: $($processInfo.Count) строк"

try {
    $cim = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $pattern } |
        Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine
    $cim | Format-List | Out-String -Width 400 |
        Set-Content -LiteralPath (Join-Path $textDir "processes-cim.txt") -Encoding UTF8
} catch {
    Write-Report "  [!] Сведения о командной строке недоступны: $($_.Exception.Message)"
}

# --------------------------------------------------------------------------
# 3. Снимки экрана
# --------------------------------------------------------------------------
if (-not $NoScreenshots) {
    Write-Report ""
    Write-Report "[3] Снимки экрана"
    try {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $index = 0
        foreach ($screen in [System.Windows.Forms.Screen]::AllScreens) {
            $index++
            $bounds = $screen.Bounds
            $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
            $path = Join-Path $screenDir ("screen-{0}.png" -f $index)
            $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
            $graphics.Dispose()
            $bitmap.Dispose()
            Write-Report "  $path"
        }
    } catch {
        Write-Report "  [!] Снимки экрана не сделаны: $($_.Exception.Message)"
    }
}

# --------------------------------------------------------------------------
# 4. Состав установленного комплекса
# --------------------------------------------------------------------------
Write-Report ""
Write-Report "[4] Установленные компоненты и файлы данных"

$roots = @(
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)},
    $env:APPDATA,
    $env:LOCALAPPDATA,
    (Join-Path $env:USERPROFILE "Documents"),
    "C:\RadioInspectorData"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$installed = New-Object System.Collections.Generic.List[string]
$dataFiles = New-Object System.Collections.Generic.List[string]
foreach ($root in $roots) {
    try {
        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "RadioInspector|Inspector|RInspector|Кассандра|Kassandra" } |
            ForEach-Object {
                $installed.Add("Каталог: $($_.FullName)")
                Get-ChildItem -LiteralPath $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Length -gt 0 } |
                    Sort-Object Length -Descending |
                    Select-Object -First 60 |
                    ForEach-Object {
                        $installed.Add(("    {0,-10} {1,12} {2}" -f $_.Extension, $_.Length, $_.FullName))
                    }
            }
    } catch {
        $installed.Add("[!] $root : $($_.Exception.Message)")
    }
}
Save-Text -Name "installed.txt" -Content ($installed -join "`n")
Write-Report "  Строк об установленном комплексе: $($installed.Count)"

$dataDirs = @(
    (Join-Path $env:USERPROFILE "Documents"),
    "C:\RadioInspectorData"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
foreach ($dir in $dataDirs) {
    Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match "^\.(pan|fcs|fdb|ifdb|inrms|imeas|itaskdb|ifrqdb|ini|txt|log|xml|json|cfg)$" } |
        ForEach-Object {
            $dataFiles.Add(("{0,12}  {1:yyyy-MM-dd HH:mm}  {2}" -f $_.Length, $_.LastWriteTime, $_.FullName))
        }
}
Save-Text -Name "data-files.txt" -Content ($dataFiles -join "`n")
Write-Report "  Файлов данных и конфигурации: $($dataFiles.Count)"

# Содержимое небольших текстовых конфигураций — только чтение, только мелкие файлы
$configDump = New-Object System.Collections.Generic.List[string]
foreach ($dir in $dataDirs) {
    Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 65536 -and $_.Extension -match "^\.(ini|txt|cfg|xml|json)$" } |
        Select-Object -First 80 |
        ForEach-Object {
            $configDump.Add("===== $($_.FullName)")
            try {
                $configDump.Add((Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue))
            } catch {
                $configDump.Add("[!] не прочитан: $($_.Exception.Message)")
            }
        }
}
if ($configDump.Count -gt 0) {
    Save-Text -Name "config-dump.txt" -Content ($configDump -join "`n")
    Write-Report "  Файлов с выгруженным содержимым: $(($configDump | Where-Object { $_ -like '=====*' }).Count)"
}

# --------------------------------------------------------------------------
# 5. Реестр: только чтение
# --------------------------------------------------------------------------
Write-Report ""
Write-Report "[5] Ключи реестра (только чтение)"
$registry = New-Object System.Collections.Generic.List[string]
foreach ($root in @("HKLM:\SOFTWARE", "HKCU:\SOFTWARE", "HKLM:\SOFTWARE\WOW6432Node")) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    try {
        Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue |
            Where-Object { $_.PSChildName -match "RadioInspector|Inspector|RInspector|RadioSoft|Laboratory" } |
            ForEach-Object {
                $registry.Add("### $($_.Name)")
                try {
                    Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue |
                        ForEach-Object {
                            $_.PSObject.Properties |
                                Where-Object { $_.Name -notmatch "^PS" } |
                                ForEach-Object { $registry.Add("    $($_.Name) = $($_.Value)") }
                        }
                    Get-ChildItem -LiteralPath $_.PSPath -Recurse -ErrorAction SilentlyContinue |
                        Select-Object -First 40 |
                        ForEach-Object { $registry.Add("    [+] $($_.Name)") }
                } catch {
                    $registry.Add("    [!] $($_.Exception.Message)")
                }
            }
    } catch {
        $registry.Add("[!] $root : $($_.Exception.Message)")
    }
}
Save-Text -Name "registry.txt" -Content ($registry -join "`n")
Write-Report "  Строк о реестре: $($registry.Count)"

# --------------------------------------------------------------------------
# 6. Журналы
# --------------------------------------------------------------------------
Write-Report ""
Write-Report "[6] Журналы работы программы"
$logs = New-Object System.Collections.Generic.List[string]
foreach ($dir in $dataDirs) {
    Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -match "^\.(log|txt)$" -and $_.Length -lt 1048576 } |
        Select-Object -First 40 |
        ForEach-Object {
            $logs.Add("===== $($_.FullName) ($($_.Length) байт)")
            try {
                $logs.Add((Get-Content -LiteralPath $_.FullName -Tail 400 -ErrorAction SilentlyContinue | Out-String))
            } catch {
                $logs.Add("[!] $($_.Exception.Message)")
            }
        }
}
if ($logs.Count -gt 0) {
    Save-Text -Name "logs.txt" -Content ($logs -join "`n")
    Write-Report "  Журналов прочитано: $(($logs | Where-Object { $_ -like '=====*' }).Count)"
} else {
    Write-Report "  Журналы не найдены"
}

# --------------------------------------------------------------------------
# 7. Манифест
# --------------------------------------------------------------------------
$manifest = [ordered]@{
    collected_at = (Get-Date).ToString("o")
    computer = $env:COMPUTERNAME
    user = $env:USERNAME
    process_filter = $ProcessFilter
    output_root = $root
    windows_total = @($windows).Count
    windows_of_original = $originalWindows
    window_rows_file = "windows.csv"
    process_count = $processInfo.Count
    screenshots = @(Get-ChildItem -LiteralPath $screenDir -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    data_file_count = $dataFiles.Count
    notes = @(
        "Снимки содержат изображение коммерческой программы и не подлежат публикации в открытом репозитории.",
        "Скрипт ничего не изменял на машине наблюдения."
    )
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $root "manifest.json") -Encoding UTF8

$report | Set-Content -LiteralPath (Join-Path $root "report.txt") -Encoding UTF8
Write-Report ""
Write-Report "Готово: $root"
Write-Report "Отправьте этот каталог (архивом) для разбора интерфейса и функциональности."
