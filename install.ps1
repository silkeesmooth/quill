<#
Installs Quill for the current Windows user. Nothing needs admin rights.

  - sets up a private Python environment in .venv (needs Python 3.10+)
  - adds a `quill` command to your PATH
  - adds "Fill PDF form" to the right-click Send To menu and the Start menu

Run:        powershell -ExecutionPolicy Bypass -File install.ps1
Uninstall:  powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [switch]$NoPath,
    [switch]$NoMenu,
    [string]$SendToDir = [Environment]::GetFolderPath('SendTo'),
    [string]$StartMenuDir = [Environment]::GetFolderPath('Programs')
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$bin = Join-Path $here 'bin'
$exe = Join-Path $bin 'quill.exe'
$ico = Join-Path $here 'quill.ico'   # made by tools\make_icon.py
# Each shortcut: where it goes, what it runs, what it says.
$shortcuts = @(
    @{ Path = (Join-Path $SendToDir 'Fill PDF form.lnk'); Args = '--pause --open'; Text = 'Answer a PDF form''s questions' },
    @{ Path = (Join-Path $StartMenuDir 'Fill PDF form.lnk'); Args = '--pause --open'; Text = 'Answer a PDF form''s questions' },
    @{ Path = (Join-Path $SendToDir 'Make PDF fillable.lnk'); Args = 'make-fillable --open --pause'; Text = 'Add typeable fields to a flat or scanned PDF form' },
    @{ Path = (Join-Path $SendToDir 'Sign PDF.lnk'); Args = 'sign --open --pause'; Text = 'Put your saved signature on a PDF form' },
    @{ Path = (Join-Path $SendToDir 'Auto-fill PDF.lnk'); Args = 'autofill --open --pause'; Text = 'Fill a PDF form from your saved details, no questions' },
    @{ Path = (Join-Path $StartMenuDir 'My details for PDF forms.lnk'); Args = 'profile --pause'; Text = 'Save your name, address and phone for auto-fill' }
)
$links = @($shortcuts | ForEach-Object { $_.Path })

# Right-click entries for PDFs. On Windows 11 they're in the full menu:
# Shift+right-click, or right-click > Show more options. They're plain entries,
# not a submenu (Windows 11's Explorer drops registry-built submenus), and they
# go both under "all PDFs" and under your default PDF app (e.g. ChromePDF),
# next to its "Open": some Explorer builds only read the latter.
$classes = 'HKCU:\Software\Classes'
$choice = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice' -ErrorAction SilentlyContinue
$pdfShells = @(Join-Path $classes 'SystemFileAssociations\.pdf\shell')
if ($choice -and $choice.ProgId) { $pdfShells += Join-Path $classes ($choice.ProgId + '\shell') }
# places an older install may have used, cleaned up on install and uninstall
$oldShells = @('ChromePDF', 'MSEdgePDF', 'AcroExch.Document.DC', 'Acrobat.Document.DC') |
    ForEach-Object { Join-Path $classes ($_ + '\shell') }
$menuItems = @(
    @{ Key = 'quill1fill'; Text = 'Fill in with Quill'; Args = '--pause --open' },
    @{ Key = 'quill2autofill'; Text = 'Auto-fill with Quill'; Args = 'autofill --open --pause' },
    @{ Key = 'quill3sign'; Text = 'Sign with Quill'; Args = 'sign --open --pause' }
)

function Remove-PdfMenu {
    foreach ($shell in @($pdfShells + $oldShells | Select-Object -Unique)) {
        foreach ($k in @(Get-ChildItem -LiteralPath $shell -ErrorAction SilentlyContinue |
                         Where-Object { $_.PSChildName -like 'quill*' -or $_.PSChildName -like 'pdffill*' })) {
            Remove-Item -LiteralPath $k.PSPath -Recurse -Force -Confirm:$false
        }
    }
}

function Update-ShellMenus {
    # Tell Explorer file menus changed (SHCNE_ASSOCCHANGED); otherwise an
    # Explorer that's already open keeps showing its old right-click menu.
    $sig = '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int e, uint f, IntPtr a, IntPtr b);'
    $t = Add-Type -MemberDefinition $sig -Name 'Shell' -Namespace 'QuillInstall' -PassThru
    $t::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
}

function Get-UserPath { [Environment]::GetEnvironmentVariable('Path', 'User') }
function Set-UserPath($value) { [Environment]::SetEnvironmentVariable('Path', $value, 'User') }
function Split-PathList($value) { @($value -split ';' | Where-Object { $_ -ne '' }) }

if ($Uninstall) {
    foreach ($l in $links) { if (Test-Path $l) { Remove-Item $l -Confirm:$false } }
    if (-not $NoPath) {
        $parts = Split-PathList (Get-UserPath)
        $kept = @($parts | Where-Object { $_.TrimEnd('\') -ne $bin.TrimEnd('\') })
        if ($kept.Count -ne $parts.Count) { Set-UserPath ($kept -join ';') }
    }
    if (-not $NoMenu) {
        Remove-PdfMenu
        Update-ShellMenus
    }
    if (Test-Path $bin) { Remove-Item $bin -Recurse -Confirm:$false }
    Write-Host 'quill shortcuts, menu and PATH entry removed. Delete this folder to remove the rest.'
    exit 0
}

# 1. Python environment
$venvPy = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $py = $null
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )
    foreach ($cand in $candidates) {
        if (-not (Get-Command $cand.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ok = & $cand.Exe @($cand.Args) -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null
            if ("$ok".Trim() -eq 'True') { $py = $cand; break }
        } catch { }
    }
    if (-not $py) {
        Write-Host 'Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/ and run this again.'
        exit 1
    }
    Write-Host 'Setting up Python environment...'
    & $py.Exe @($py.Args) -m venv (Join-Path $here '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
Write-Host 'Installing quill...'
& $venvPy -m pip install --quiet --disable-pip-version-check -e $here
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

# 2. One exe on PATH (not the whole venv, which would shadow your `python`)
New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item (Join-Path $here '.venv\Scripts\quill.exe') $exe -Force
if (-not $NoPath) {
    $parts = Split-PathList (Get-UserPath)
    if (-not ($parts | Where-Object { $_.TrimEnd('\') -eq $bin.TrimEnd('\') })) {
        Set-UserPath ((@($parts) + $bin) -join ';')
    }
}

# 3. Send To and Start menu shortcuts
$shell = New-Object -ComObject WScript.Shell
foreach ($l in $links) {
    New-Item -ItemType Directory -Force (Split-Path $l) | Out-Null
    $spec = $shortcuts | Where-Object { $_.Path -eq $l }
    $s = $shell.CreateShortcut($l)
    $s.TargetPath = $exe
    if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
    $s.Arguments = $spec.Args
    $s.WorkingDirectory = $HOME
    $s.Description = $spec.Text
    $s.Save()
}

# 4. Right-click entries for PDFs (per user, no admin needed)
if (-not $NoMenu) {
    Remove-PdfMenu  # also clears the old submenu from 1.4
    foreach ($shell in $pdfShells) {
        foreach ($item in $menuItems) {
            $key = Join-Path $shell $item.Key
            New-Item -Path (Join-Path $key 'command') -Force | Out-Null
            Set-ItemProperty -LiteralPath $key -Name 'MUIVerb' -Value $item.Text
            $icon = if (Test-Path $ico) { $ico } else { $exe }
            Set-ItemProperty -LiteralPath $key -Name 'Icon' -Value ('"' + $icon + '"')
            Set-Item -LiteralPath (Join-Path $key 'command') -Value ('"' + $exe + '" ' + $item.Args + ' "%1"')
        }
    }
    Update-ShellMenus
}
Write-Host ''
Write-Host 'Done. Ways to use it:'
Write-Host '  - Shift+right-click a PDF (or Show more options) > Fill in / Auto-fill / Sign with Quill'
Write-Host '  - Right-click a PDF > Send to > Fill PDF form'
Write-Host '  - Right-click a flat or scanned PDF > Send to > Make PDF fillable'
Write-Host '  - Right-click a PDF > Send to > Sign PDF'
Write-Host '  - Right-click a PDF > Send to > Auto-fill PDF (your details, no questions)'
Write-Host '  - Start menu > My details for PDF forms (save your info once)'
Write-Host '  - Start menu > Fill PDF form, then drag a PDF into the window'
Write-Host '  - In a new terminal: quill form.pdf   (quill -h for more)'
