#Requires -Version 5.1
<#
.SYNOPSIS
  Read-only host prerequisite check for Grok Bot-Build Bridge (Windows).

.DESCRIPTION
  Built-in commands only. Does NOT install, does NOT start stopped WSL distros,
  does NOT assert companion package version, does NOT read secrets.
  All probes use bounded timeouts and capped output.

  Exit 0 = script completed (report may still show missing prereqs).
  Exit 2 = unexpected failure / self-test failure.
#>
[CmdletBinding()]
param(
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$script:DefaultTimeoutSec = 8
$script:DefaultMaxBytes = 65536
$script:MinPythonMajor = 3
$script:MinPythonMinor = 11

function Test-IsWindowsAppsAliasPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $n = $Path.Replace('/', '\')
    return ($n -match '(?i)\\WindowsApps\\') -or ($n -match '(?i)\\Microsoft\\WindowsApps\\')
}

function ConvertFrom-CliBytes {
    param([byte[]]$Bytes)
    if ($null -eq $Bytes -or $Bytes.Length -eq 0) { return '' }
    $head = [Math]::Min(64, $Bytes.Length)
    $hasNul = $false
    for ($i = 0; $i -lt $head; $i++) {
        if ($Bytes[$i] -eq 0) { $hasNul = $true; break }
    }
    if (($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xFE) -or $hasNul) {
        try {
            return [System.Text.Encoding]::Unicode.GetString($Bytes).Replace([char]0, '')
        } catch {
            return [System.Text.Encoding]::UTF8.GetString($Bytes).Replace([char]0, '')
        }
    }
    if ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFE -and $Bytes[1] -eq 0xFF) {
        return [System.Text.Encoding]::BigEndianUnicode.GetString($Bytes).Replace([char]0, '')
    }
    return [System.Text.Encoding]::UTF8.GetString($Bytes).Replace([char]0, '')
}

function Invoke-BoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [int]$TimeoutSec = $script:DefaultTimeoutSec,
        [int]$MaxBytes = $script:DefaultMaxBytes
    )
    $result = [ordered]@{
        timed_out = $false
        exit_code = $null
        stdout = ''
        stderr = ''
        error = $null
        truncated = $false
    }
    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        $result.error = 'empty_file_path'
        return $result
    }
    if (-not (Test-Path -LiteralPath $FilePath)) {
        # Still allow bare names resolved by CreateProcess PATH search via FileName
        if ($FilePath -match '[\\/]' ) {
            $result.error = 'executable_not_found'
            return $result
        }
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    # ArgumentList joining: quote only when needed; for our fixed argv this is fine.
    if ($ArgumentList -and $ArgumentList.Count -gt 0) {
        $psi.Arguments = (($ArgumentList | ForEach-Object {
            $a = [string]$_
            if ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' } else { $a }
        }) -join ' ')
    }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try {
        [void]$proc.Start()
    } catch {
        $result.error = 'start_failed'
        return $result
    }

    # Async read into MemoryStreams to support binary/UTF-16 from WSL.
    $outMs = New-Object System.IO.MemoryStream
    $errMs = New-Object System.IO.MemoryStream
    $outDone = $proc.StandardOutput.BaseStream.CopyToAsync($outMs)
    $errDone = $proc.StandardError.BaseStream.CopyToAsync($errMs)
    $finished = $proc.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) {
        $result.timed_out = $true
        $result.error = 'timeout'
        try { $proc.Kill() } catch {}
        try { [void]$proc.WaitForExit(2000) } catch {}
    } else {
        $result.exit_code = $proc.ExitCode
    }
    try { [void]$outDone.Wait(500) } catch {}
    try { [void]$errDone.Wait(500) } catch {}
    $outBytes = $outMs.ToArray()
    $errBytes = $errMs.ToArray()
    if ($outBytes.Length -gt $MaxBytes) {
        $outBytes = $outBytes[0..($MaxBytes - 1)]
        $result.truncated = $true
    }
    if ($errBytes.Length -gt $MaxBytes) {
        $errBytes = $errBytes[0..($MaxBytes - 1)]
        $result.truncated = $true
    }
    $result.stdout = ConvertFrom-CliBytes -Bytes $outBytes
    $result.stderr = ConvertFrom-CliBytes -Bytes $errBytes
    try { $outMs.Dispose(); $errMs.Dispose(); $proc.Dispose() } catch {}
    return $result
}

function Test-PythonVersionAtLeast311 {
    param([string]$VersionText)
    if ($VersionText -notmatch '^(\d+)\.(\d+)\.(\d+)$') { return $false }
    $maj = [int]$Matches[1]; $min = [int]$Matches[2]
    if ($maj -gt $script:MinPythonMajor) { return $true }
    if ($maj -lt $script:MinPythonMajor) { return $false }
    return ($min -ge $script:MinPythonMinor)
}

function Get-PythonProbe {
    # Each candidate keeps an exact argv *prefix* used for all follow-up -c probes.
    $candidates = @(
        @{ Label = 'py-3'; File = 'py'; Prefix = @('-3') },
        @{ Label = 'python'; File = 'python'; Prefix = @() },
        @{ Label = 'python3'; File = 'python3'; Prefix = @() }
    )
    $skippedAliases = @()
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.File -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $source = [string]$cmd.Source
        if (Test-IsWindowsAppsAliasPath -Path $source) {
            $skippedAliases += $source
            continue
        }
        # Resolve FilePath for CreateProcess: prefer Source when it is a real file.
        $filePath = $source
        if ([string]::IsNullOrWhiteSpace($filePath) -or -not (Test-Path -LiteralPath $filePath)) {
            $filePath = $c.File
        }
        $verArgs = @()
        $verArgs += $c.Prefix
        $verArgs += '--version'
        $ver = Invoke-BoundedProcess -FilePath $filePath -ArgumentList $verArgs -TimeoutSec 8
        if ($ver.timed_out -or $ver.error) { continue }
        $text = (($ver.stdout + "`n" + $ver.stderr) -replace '\r', '').Trim()
        if ($text -notmatch 'Python\s+(\d+\.\d+\.\d+)') { continue }
        $version = $Matches[1]
        if (-not (Test-PythonVersionAtLeast311 -VersionText $version)) {
            continue
        }
        # Resolve the *actual* interpreter via sys.executable using the SAME argv prefix.
        $resArgs = @()
        $resArgs += $c.Prefix
        $resArgs += @('-c', 'import sys; print(sys.version.split()[0]); print(sys.executable)')
        $resolved = Invoke-BoundedProcess -FilePath $filePath -ArgumentList $resArgs -TimeoutSec 8
        if ($resolved.timed_out -or $resolved.error -or $resolved.exit_code -ne 0) {
            continue
        }
        $lines = @(($resolved.stdout -replace '\r', '').Trim() -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if ($lines.Count -lt 2) { continue }
        $exe = $lines[1]
        if (Test-IsWindowsAppsAliasPath -Path $exe) {
            $skippedAliases += $exe
            continue
        }
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        $ver2 = $lines[0]
        if (-not (Test-PythonVersionAtLeast311 -VersionText $ver2)) { continue }
        return [ordered]@{
            present = $true
            meets_min = $true
            min_required = '3.11'
            label = $c.Label
            version = $ver2
            launcher = $filePath
            argv_prefix = @(@($c.File) + @($c.Prefix))
            executable = $exe
            note = 'interpreter_ge_311_sys_executable_resolved'
            windowsapps_aliases_skipped = $skippedAliases
        }
    }
    $note = 'no_python_311_plus_probe_matched'
    if ($skippedAliases.Count -gt 0 -and -not $note) { }
    if ($skippedAliases.Count -gt 0) {
        $note = 'no_python_311_plus_probe_matched_windowsapps_aliases_skipped'
    }
    return [ordered]@{
        present = $false
        meets_min = $false
        min_required = '3.11'
        label = $null
        version = $null
        launcher = $null
        argv_prefix = @()
        executable = $null
        note = $note
        windowsapps_aliases_skipped = $skippedAliases
    }
}

function Get-VenvModuleProbe {
    param(
        [string]$Executable
    )
    if ([string]::IsNullOrWhiteSpace($Executable)) {
        return [ordered]@{ present = $false; note = 'skipped_no_python' }
    }
    if (Test-IsWindowsAppsAliasPath -Path $Executable) {
        return [ordered]@{ present = $false; note = 'refused_windowsapps_alias' }
    }
    # Call the resolved sys.executable directly (no launcher prefix needed).
    $r = Invoke-BoundedProcess -FilePath $Executable -ArgumentList @('-c', 'import venv; print("venv_ok")') -TimeoutSec 8
    if ($r.timed_out) {
        return [ordered]@{ present = $false; note = 'venv_probe_timeout' }
    }
    if ($r.exit_code -eq 0 -and ($r.stdout -match 'venv_ok')) {
        return [ordered]@{ present = $true; note = 'stdlib_venv_import_ok' }
    }
    return [ordered]@{ present = $false; note = 'venv_import_failed' }
}

function Get-CompanionProbe {
    $names = @('grok-bot-build-bridge', 'grok-bot-build-bridge.exe')
    foreach ($n in $names) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $source = [string]$cmd.Source
        if ([string]::IsNullOrWhiteSpace($source)) {
            return [ordered]@{
                status = 'unknown'
                path = $null
                version = $null
                note = 'command_found_but_source_unresolved_version_not_queried'
            }
        }
        return [ordered]@{
            status = 'present_on_path'
            path = $source
            version = $null
            note = 'on_path_but_version_not_queried_in_prereq_first_proof'
        }
    }
    return [ordered]@{
        status = 'absent'
        path = $null
        version = $null
        note = 'companion_wheel_not_required_for_this_check'
    }
}

function Parse-WslListVerboseText {
    <#
      Parity with grok_bot_build_bridge.discovery.parse_wsl_list_verbose:
      - strip NULs/BOM
      - English states only (Running|Stopped|Installing|Uninstalling)
      - name must match ^[A-Za-z0-9._-]{1,64}$ (no spaces)
      - do NOT invent readiness for unknown/localized lines
    #>
    param([string]$Text)
    $distros = @()
    $skipped = @()
    $stopped = $false
    $cleaned = ($Text -replace "`0", '').Replace([char]0xFEFF, '')
    foreach ($raw in ($cleaned -split "`n")) {
        $line = ($raw -replace '\r', '').Trim()
        if (-not $line) { continue }
        $low = $line.ToLowerInvariant()
        if ($low.StartsWith('name') -and $low.Contains('state')) { continue }
        if ($low -eq 'name') { continue }
        $line2 = [regex]::Replace($line, '\s+', ' ')
        $m = [regex]::Match(
            $line2,
            '^(?<star>\*)?\s*(?<name>[A-Za-z0-9._-]{1,64})\s+(?<state>Running|Stopped|Installing|Uninstalling)\s+(?<ver>\d+)\s*$',
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
        if (-not $m.Success) {
            $skipped += [ordered]@{
                line = $line2.Substring(0, [Math]::Min(120, $line2.Length))
                reason = 'unparsed_localized_or_unsafe_or_malformed'
            }
            continue
        }
        $state = $m.Groups['state'].Value.Substring(0,1).ToUpper() + $m.Groups['state'].Value.Substring(1).ToLower()
        if ($state -eq 'Stopped') { $stopped = $true }
        $distros += [ordered]@{
            name = $m.Groups['name'].Value
            state = $state
            version = $m.Groups['ver'].Value
            default = [bool]$m.Groups['star'].Value
            note = if ($state -eq 'Stopped') { 'stopped_ask_operator_before_start' } else { $null }
            readiness = if ($state -eq 'Running') { 'listed_running_not_probed' } else { 'not_ready_from_list_alone' }
        }
    }
    return [ordered]@{
        distros = $distros
        skipped_lines = $skipped
        stopped_present = $stopped
    }
}

function Get-WslProbe {
    param(
        # Optional injection for self-test / CI mocks (raw text already decoded).
        [string]$MockListText = $null,
        [switch]$MockTimeout,
        [switch]$MockAbsent
    )
    if ($MockAbsent) {
        return [ordered]@{
            exe_present = $false
            distros = @()
            skipped_lines = @()
            stopped_present = $false
            note = 'wsl_exe_absent'
            never_auto_start = $true
        }
    }
    if ($MockTimeout) {
        return [ordered]@{
            exe_present = $true
            distros = @()
            skipped_lines = @()
            stopped_present = $false
            note = 'wsl_list_timed_out'
            never_auto_start = $true
            timed_out = $true
        }
    }
    if ($null -ne $MockListText) {
        $parsed = Parse-WslListVerboseText -Text $MockListText
        return [ordered]@{
            exe_present = $true
            distros = $parsed.distros
            skipped_lines = $parsed.skipped_lines
            stopped_present = $parsed.stopped_present
            note = 'list_only_never_started_stopped_mock'
            never_auto_start = $true
        }
    }

    $wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
    if (-not $wsl) { $wsl = Get-Command 'wsl' -ErrorAction SilentlyContinue }
    if (-not $wsl) {
        return [ordered]@{
            exe_present = $false
            distros = @()
            skipped_lines = @()
            stopped_present = $false
            note = 'wsl_exe_absent'
            never_auto_start = $true
        }
    }
    $filePath = [string]$wsl.Source
    if ([string]::IsNullOrWhiteSpace($filePath)) { $filePath = 'wsl.exe' }
    # List only - never wsl -d (would auto-start stopped distros).
    $raw = Invoke-BoundedProcess -FilePath $filePath -ArgumentList @('-l', '-v') -TimeoutSec 12
    if ($raw.timed_out) {
        return [ordered]@{
            exe_present = $true
            distros = @()
            skipped_lines = @()
            stopped_present = $false
            note = 'wsl_list_timed_out'
            never_auto_start = $true
            timed_out = $true
        }
    }
    if ($raw.error -and $raw.error -ne $null -and $raw.exit_code -eq $null -and -not $raw.stdout) {
        return [ordered]@{
            exe_present = $true
            distros = @()
            skipped_lines = @()
            stopped_present = $false
            note = 'wsl_list_failed'
            never_auto_start = $true
            error = $raw.error
        }
    }
    $parsed = Parse-WslListVerboseText -Text ($raw.stdout + "`n" + $raw.stderr)
    return [ordered]@{
        exe_present = $true
        distros = $parsed.distros
        skipped_lines = $parsed.skipped_lines
        stopped_present = $parsed.stopped_present
        note = 'list_only_never_started_stopped'
        never_auto_start = $true
    }
}

function New-HostPrereqReport {
    param($Python, $Venv, $Companion, $Wsl)
    return [ordered]@{
        schema_version = 1
        check_kind = 'host_prereq_builtin'
        runtime_kind = 'windows_host'
        runtime_note = 'This script is intended for the selected Windows desktop (Grok Bot local execution). It is not the Bot BOX Linux environment.'
        authority_note = 'Same Grok Bot account / registered machine does not authorize execution beyond the operator approval for this request. Denied approval: stop; do not reroute.'
        offline_note = 'If ListMachines shows connected=false, local execution is blocked until the desktop app is open and online.'
        python = $Python
        venv = $Venv
        companion_cli = $Companion
        wsl = $Wsl
        secrets = 'none_read_none_written'
        next_operator_steps = @(
            'If python.present is false or meets_min is false: install Python 3.11+ (operator-managed), avoid WindowsApps store aliases, then re-run this check.',
            'If companion_cli.status is absent: after operator approval, install the approved wheel into an isolated venv (see docs/LOCAL_EXEC.md) - no curl|pipe, no global install.',
            'If wsl shows Stopped distros: ask the operator to start them; never auto-start. Skipped/unparsed lines are not treated as ready.',
            'Preserve webhook pairing as a separate one-way notify path; do not conflate with local exec.'
        )
    }
}

function Invoke-HostPrereqSelfTest {
    $failures = @()

    # Version gate
    if (Test-PythonVersionAtLeast311 -VersionText '3.10.9') { $failures += '3.10 should fail min' }
    if (-not (Test-PythonVersionAtLeast311 -VersionText '3.11.0')) { $failures += '3.11.0 should pass' }
    if (-not (Test-PythonVersionAtLeast311 -VersionText '3.12.1')) { $failures += '3.12 should pass' }

    # WindowsApps detection
    if (-not (Test-IsWindowsAppsAliasPath -Path 'C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe')) {
        $failures += 'windowsapps detect failed'
    }
    if (Test-IsWindowsAppsAliasPath -Path 'C:\Python312\python.exe') {
        $failures += 'false positive windowsapps'
    }

    # WSL UTF-16 NULs + English row
    # Simulate NUL-heavy decode path via ConvertFrom-CliBytes
    $bytes = [System.Text.Encoding]::Unicode.GetBytes("  NAME      STATE           VERSION`r`n* Ubuntu    Running         2`r`n")
    $decoded = ConvertFrom-CliBytes -Bytes $bytes
    $p1 = Parse-WslListVerboseText -Text $decoded
    if ($p1.distros.Count -ne 1 -or $p1.distros[0].name -ne 'Ubuntu' -or $p1.distros[0].state -ne 'Running') {
        $failures += 'utf16 english parse failed'
    }

    # Localized / unknown state must NOT invent readiness
    $loc = "  NAME      STATE           VERSION`n* Ubuntu    Wird_ausgefuehrt         2`n"
    $p2 = Parse-WslListVerboseText -Text $loc
    if ($p2.distros.Count -ne 0) { $failures += 'localized state should not parse as ready' }
    if ($p2.skipped_lines.Count -lt 1) { $failures += 'localized line should be skipped' }

    # Distro name with spaces -> unsafe / unparsed
    $space = "* My Distro    Running         2`n"
    $p3 = Parse-WslListVerboseText -Text $space
    if ($p3.distros.Count -ne 0) { $failures += 'spaced name must not parse' }

    # Stopped
    $st = "* Ubuntu    Stopped         2`n"
    $p4 = Parse-WslListVerboseText -Text $st
    if (-not $p4.stopped_present) { $failures += 'stopped_present expected' }
    if ($p4.distros[0].readiness -ne 'not_ready_from_list_alone') { $failures += 'stopped readiness wrong' }

    # Mock timeout path
    $to = Get-WslProbe -MockTimeout
    if ($to.note -ne 'wsl_list_timed_out' -or -not $to.never_auto_start) { $failures += 'mock timeout failed' }

    # Malformed header-only
    $p5 = Parse-WslListVerboseText -Text "NAME  STATE  VERSION`n"
    if ($p5.distros.Count -ne 0) { $failures += 'header-only should yield no distros' }

    if ($failures.Count -gt 0) {
        Write-Output (@{ ok = $false; failures = $failures } | ConvertTo-Json -Compress -Depth 6)
        exit 2
    }
    Write-Output (@{ ok = $true; selftest = 'host_prereq_ps1'; checks = 8 } | ConvertTo-Json -Compress)
    exit 0
}

if ($SelfTest) {
    Invoke-HostPrereqSelfTest
    exit 1
}

$python = Get-PythonProbe
$venv = Get-VenvModuleProbe -Executable ([string]$python.executable)
$companion = Get-CompanionProbe
$wsl = Get-WslProbe
$report = New-HostPrereqReport -Python $python -Venv $venv -Companion $companion -Wsl $wsl
$report | ConvertTo-Json -Depth 8
exit 0
