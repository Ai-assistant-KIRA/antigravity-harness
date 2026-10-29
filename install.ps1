# Harness² bootstrap installer for Windows.
#
# Works two ways:
#   1. From a checkout:   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 [installer args]
#   2. From the web:      irm https://raw.githubusercontent.com/Ai-assistant-KIRA/antigravity-harness/main/install.ps1 | iex
#
# It finds Python 3.10+, downloads the source when it is not run from a
# checkout, runs install_plugin.py (which backs up any existing install and
# keeps memory, rules, and user skills), and runs `harness doctor`.
#
# The last line is machine-readable for agents:
#   HARNESS_INSTALL: ok version=<v> target=<path>
#   HARNESS_INSTALL: failed reason=<text>
#
# Environment overrides:
#   HARNESS_PYTHON        interpreter to use
#   HARNESS_SOURCE_URL    ZIP archive to install from (default: main branch)
#   HARNESS_INSTALL_ARGS  extra installer arguments when piped through iex

function Install-Harness {
    param([string[]]$InstallerArgs = @())

    $ErrorActionPreference = 'Stop'
    $repoZip = 'https://github.com/Ai-assistant-KIRA/antigravity-harness/archive/refs/heads/main.zip'
    $minimum = [Version]'3.10'

    function Write-Step([string]$Message) { Write-Host "[harness] $Message" }

    function Get-PythonCandidate {
        $candidates = @()
        if ($env:HARNESS_PYTHON) { $candidates += , @($env:HARNESS_PYTHON) }
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', '-3') }
        foreach ($name in 'python', 'python3') {
            $command = Get-Command $name -ErrorAction SilentlyContinue
            # The Microsoft Store alias in WindowsApps only opens the Store.
            if ($command -and $command.Source -notmatch '\\WindowsApps\\') { $candidates += , @($command.Source) }
        }
        foreach ($candidate in $candidates) {
            $exe = $candidate[0]
            $prefix = @()
            if ($candidate.Count -gt 1) { $prefix = $candidate[1..($candidate.Count - 1)] }
            try {
                $probe = & $exe @prefix -c "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -ne 0 -or -not $probe) { continue }
                $lines = @($probe)
                if ([Version]$lines[-1] -ge $minimum) {
                    return [pscustomobject]@{ Path = $lines[0]; Version = $lines[-1] }
                }
            } catch { continue }
        }
        return $null
    }

    $python = Get-PythonCandidate
    if (-not $python) {
        throw 'Python 3.10 or newer was not found. Install it with "winget install -e --id Python.Python.3.12" (or from https://www.python.org/downloads/), open a new terminal, and run this installer again.'
    }
    Write-Step "using Python $($python.Version) at $($python.Path)"

    $source = $null
    $cleanup = $null
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'install_plugin.py'))) {
        $source = $PSScriptRoot
        Write-Step "installing from checkout $source"
    } else {
        $url = $repoZip
        if ($env:HARNESS_SOURCE_URL) { $url = $env:HARNESS_SOURCE_URL }
        $cleanup = Join-Path ([IO.Path]::GetTempPath()) ("harness-install-" + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $cleanup | Out-Null
        $zip = Join-Path $cleanup 'source.zip'
        Write-Step "downloading $url"
        # Windows PowerShell 5.1 defaults to TLS 1.0, which GitHub rejects.
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $previousProgress = $ProgressPreference
        $ProgressPreference = 'SilentlyContinue'
        # Progress bars print tens of KB into a non-interactive console, which
        # can push the final status line out of an agent's view.
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath $cleanup -Force
        } finally { $ProgressPreference = $previousProgress }
        $found = Get-ChildItem -Path $cleanup -Filter 'install_plugin.py' -Recurse -Depth 2 | Select-Object -First 1
        if (-not $found) { throw "the downloaded archive does not contain install_plugin.py ($url)" }
        $source = $found.Directory.FullName
    }

    try {
        $installer = Join-Path $source 'install_plugin.py'
        Write-Step "running install_plugin.py $($InstallerArgs -join ' ')"
        & $python.Path -X utf8 $installer @InstallerArgs
        if ($LASTEXITCODE -ne 0) { throw "install_plugin.py exited with code $LASTEXITCODE (see the output above)" }
    } finally {
        if ($cleanup -and (Test-Path $cleanup)) { Remove-Item -Recurse -Force $cleanup -ErrorAction SilentlyContinue }
    }
}

$harnessArgs = @($args)
if ($harnessArgs.Count -eq 0 -and $env:HARNESS_INSTALL_ARGS) {
    $harnessArgs = $env:HARNESS_INSTALL_ARGS -split '\s+' | Where-Object { $_ }
}
$harnessFailed = $false
try {
    Install-Harness -InstallerArgs $harnessArgs
} catch {
    $harnessFailed = $true
    Write-Host "[harness] ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "HARNESS_INSTALL: failed reason=$($_.Exception.Message)"
}
if ($harnessFailed) {
    # A non-zero exit code matters to agents and CI. `exit` would also close a
    # user's interactive window when piped through iex, so only exit when this
    # run is a script file or a one-shot `powershell -Command` process.
    $oneShot = [Environment]::GetCommandLineArgs() | Where-Object { $_ -match '^-(c|command|file|noninteractive|encodedcommand)$' }
    if ($PSCommandPath -or $oneShot) { exit 1 }
}
