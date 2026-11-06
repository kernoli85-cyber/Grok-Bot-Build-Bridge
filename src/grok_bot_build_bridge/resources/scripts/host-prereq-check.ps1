#Requires -Version 5.1
<#
.SYNOPSIS
  Read-only host prerequisite check for Grok Bot-Build Bridge (Windows).

.DESCRIPTION
  Built-in commands only. Does NOT install anything, does NOT start stopped WSL
  distros, does NOT assert companion package version (may be absent), does NOT
  read secrets. Safe first proof for Grok Bot local execution (Shell machineId)
  before a wheel is installed.

  Exit codes: 0 = ran successfully (report may still show missing prereqs);
              2 = unexpected script failure.
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

function Get-PythonProbe {
    $candidates = @(
        @{ Label = 'py-3'; Argv = @('py', '-3', '--version') },
        @{ Label = 'python'; Argv = @('python', '--version') },
        @{ Label = 'python3'; Argv = @('python3', '--version') }
    )
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Argv[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $out = & $c.Argv[0] $c.Argv[1..($c.Argv.Length - 1)] 2>&1 | Out-String
            $text = ($out -replace '\r', '').Trim()
            if ($text -match 'Python\s+(\d+\.\d+\.\d+)') {
                return [ordered]@{
                    present = $true
                    label = $c.Label
                    version = $Matches[1]
                    executable = [string]$cmd.Source
                    note = 'interpreter_found_companion_not_asserted'
                }
            }
        } catch {
            continue
        }
    }
    return [ordered]@{
        present = $false
        label = $null
        version = $null
        executable = $null
        note = 'no_python_311_plus_probe_matched'
    }
}

function Get-VenvModuleProbe {
    param([string]$PythonExe)
    if (-not $PythonExe) {
        return [ordered]@{ present = $false; note = 'skipped_no_python' }
    }
    try {
        $code = 'import venv; print("venv_ok")'
        $out = & $PythonExe -c $code 2>&1 | Out-String
        if ($out -match 'venv_ok') {
            return [ordered]@{ present = $true; note = 'stdlib_venv_import_ok' }
        }
    } catch {}
    return [ordered]@{ present = $false; note = 'venv_import_failed' }
}

function Get-CompanionProbe {
    $names = @('grok-bot-build-bridge', 'grok-bot-build-bridge.exe')
    foreach ($n in $names) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd) {
            return [ordered]@{
                status = 'present_on_path'
                path = [string]$cmd.Source
                version = $null
                note = 'on_path_but_version_not_queried_in_prereq_first_proof'
            }
        }
    }
    return [ordered]@{
        status = 'absent'
        path = $null
        version = $null
        note = 'companion_wheel_not_required_for_this_check'
    }
}

function Get-WslProbe {
    $wsl = Get-Command 'wsl.exe' -ErrorAction SilentlyContinue
    if (-not $wsl) {
        $wsl = Get-Command 'wsl' -ErrorAction SilentlyContinue
    }
    if (-not $wsl) {
        return [ordered]@{
            exe_present = $false
            distros = @()
            stopped_present = $false
            note = 'wsl_exe_absent'
            never_auto_start = $true
        }
    }
    $raw = ''
    try {
        # List only — never wsl -d <stopped> (would auto-start).
        $raw = & $wsl.Source -l -v 2>&1 | Out-String
    } catch {
        return [ordered]@{
            exe_present = $true
            distros = @()
            stopped_present = $false
            note = 'wsl_list_failed'
            never_auto_start = $true
        }
    }
    $distros = @()
    $stopped = $false
    foreach ($line in ($raw -split "`n")) {
        $t = $line.Trim()
        if (-not $t -or $t -match '^NAME\s+STATE' -or $t -match '^-+') { continue }
        $star = $false
        if ($t.StartsWith('*')) { $star = $true; $t = $t.TrimStart('*').Trim() }
        $parts = ($t -split '\s+', 3)
        if ($parts.Count -lt 2) { continue }
        $name = $parts[0]
        $state = $parts[1]
        $ver = if ($parts.Count -ge 3) { $parts[2] } else { $null }
        if ($state -match 'Stopped') { $stopped = $true }
        $distros += [ordered]@{
            name = $name
            state = $state
            version = $ver
            default = $star
            note = if ($state -match 'Stopped') { 'stopped_ask_operator_before_start' } else { $null }
        }
    }
    return [ordered]@{
        exe_present = $true
        distros = $distros
        stopped_present = $stopped
        note = 'list_only_never_started_stopped'
        never_auto_start = $true
    }
}

$python = Get-PythonProbe
$venv = Get-VenvModuleProbe -PythonExe $python.executable
$companion = Get-CompanionProbe
$wsl = Get-WslProbe

$report = [ordered]@{
    schema_version = 1
    check_kind = 'host_prereq_builtin'
    runtime_kind = 'windows_host'
    runtime_note = 'This script is intended for the selected Windows desktop (Grok Bot local execution). It is not the Bot BOX Linux environment.'
    authority_note = 'Same Grok Bot account / registered machine does not authorize execution beyond the operator approval for this request. Denied approval: stop; do not reroute.'
    offline_note = 'If ListMachines shows connected=false, local execution is blocked until the desktop app is open and online.'
    python = $python
    venv = $venv
    companion_cli = $companion
    wsl = $wsl
    secrets = 'none_read_none_written'
    next_operator_steps = @(
        'If python.present is false: install Python 3.11+ (operator-managed), then re-run this check.',
        'If companion_cli.status is absent: after operator approval, install the approved wheel into an isolated venv (see docs/LOCAL_EXEC.md) — no curl|pipe, no global install.',
        'If wsl shows Stopped distros: ask the operator to start them; never auto-start.',
        'Preserve webhook pairing as a separate one-way notify path; do not conflate with local exec.'
    )
}

$report | ConvertTo-Json -Depth 6
exit 0
