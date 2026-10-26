<#
.SYNOPSIS
    Will security software on this PC block or flag the HyStar watcher?
    READ-ONLY -- reports, changes nothing.

.DESCRIPTION
    watch_hystar_errors.ps1 runs as a scheduled task every minute. To keep a
    console window from flashing on the acquisition PC it starts like this:

        wscript.exe //B //Nologo watch_hystar_errors.vbs
          -> powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ...

    A hidden PowerShell started from a VBScript every minute is also what a
    lot of malware looks like, so antivirus, an endpoint-protection agent
    (EDR) or an app-control policy may block it, quarantine it, or report it
    to whoever manages the PC. This script looks for each of those, reports
    how the STAN copy tasks already on this PC have been running (they use
    the same hidden-PowerShell pattern, hourly, without the VBScript step),
    and writes everything to a report on the share.

    -TryLaunch additionally starts ONE hidden PowerShell through a VBScript,
    exactly the way the watcher would (without Task Scheduler), checks that
    it ran, and looks for any Defender block in the minutes after. Run it
    only after deciding whether to tell IT first: if an EDR agent is going
    to flag the watcher, it will flag this test the same way.

.EXAMPLE
    .\check_watcher_security.ps1
    .\check_watcher_security.ps1 -TryLaunch
#>
[CmdletBinding()]
param(
    [switch] $TryLaunch
)
$ErrorActionPreference = 'Continue'

$script:Report = New-Object System.Collections.Generic.List[string]
function Say($m, $c = 'Gray') {
    Write-Host $m -ForegroundColor $c
    $script:Report.Add([string]$m)
}
function Section($title) {
    Say ''
    Say "=== $title ===" 'Cyan'
}
function Short($text, $max) {
    $t = [string]$text
    $t = $t -replace '\s+', ' '
    if ($t.Length -gt $max) { $t = $t.Substring(0, $max) }
    return $t
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
Say "check_watcher_security on $env:COMPUTERNAME at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

# ------------------------------------------------------------------ 1. host
Section '1. This PC'
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    Say "Windows: $($os.Caption) $($os.Version) (build $($os.BuildNumber))"
} catch { Say "Windows version: unreadable ($($_.Exception.Message))" 'Yellow' }
Say "PowerShell: $($PSVersionTable.PSVersion)"
try {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Say "Running as: $($id.Name)   elevated: $isAdmin"
} catch { Say 'Running as: unreadable' 'Yellow' }
try {
    $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
    Say "Domain joined: $($cs.PartOfDomain)   domain/workgroup: $($cs.Domain)"
} catch { Say 'Domain membership: unreadable' 'Yellow' }
try {
    $ds = & dsregcmd.exe /status 2>$null
    foreach ($line in $ds) {
        if ($line -match 'AzureAdJoined|DomainJoined|EnterpriseJoined|TenantName|MdmUrl') { Say ("  {0}" -f $line.Trim()) }
    }
} catch { Say '  dsregcmd: not available' }
$enroll = 'HKLM:\SOFTWARE\Microsoft\Enrollments'
if (Test-Path $enroll) {
    $n = 0
    foreach ($k in @(Get-ChildItem $enroll -ErrorAction SilentlyContinue)) {
        $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
        if ($p -and $p.ProviderID) { $n++ ; Say "  MDM enrollment: $($p.ProviderID)" }
    }
    if ($n -eq 0) { Say '  MDM enrollment: none found' }
}

# ------------------------------------------------- 2. antivirus registered
Section '2. Antivirus products registered with Windows'
try {
    $av = @(Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction Stop)
    if ($av.Count -eq 0) { Say 'none registered' }
    foreach ($a in $av) { Say ("{0}   (productState {1})" -f $a.displayName, $a.productState) }
} catch { Say 'SecurityCenter2 not available (normal on Windows Server)' 'Yellow' }

# ---------------------------------------------------- 3. security agents
Section '3. Security / EDR agents running'
$known = @(
    @('MsMpEng', 'Microsoft Defender Antivirus'),
    @('MsSense', 'Microsoft Defender for Endpoint (EDR)'),
    @('SenseCncProxy', 'Microsoft Defender for Endpoint (EDR)'),
    @('CSFalconService', 'CrowdStrike Falcon (EDR)'),
    @('SentinelAgent', 'SentinelOne (EDR)'),
    @('cyserver', 'Palo Alto Cortex XDR (EDR)'),
    @('RepMgr', 'Carbon Black (EDR)'),
    @('SophosHealth', 'Sophos'),
    @('SAVService', 'Sophos'),
    @('mfemms', 'McAfee / Trellix'),
    @('masvc', 'McAfee / Trellix'),
    @('xagt', 'Trellix / FireEye HX (EDR)'),
    @('ccSvcHst', 'Symantec'),
    @('ekrn', 'ESET'),
    @('elastic-endpoint', 'Elastic Defend (EDR)'),
    @('TaniumClient', 'Tanium'),
    @('QualysAgent', 'Qualys'),
    @('BESClient', 'HCL BigFix'),
    @('ir_agent', 'Rapid7 Insight'),
    @('wazuh-agent', 'Wazuh')
)
$running = @{}
foreach ($pr in @(Get-Process -ErrorAction SilentlyContinue)) { $running[$pr.ProcessName.ToLower()] = $true }
$hits = 0
foreach ($k in $known) {
    if ($running.ContainsKey($k[0].ToLower())) { $hits++ ; Say ("FOUND  {0,-18} {1}" -f $k[0], $k[1]) 'Yellow' }
}
if ($hits -eq 0) { Say 'none of the common agents found' }
Say 'Services whose name suggests security software:'
$pattern = 'Defender|Sense|Falcon|Sentinel|Cortex|Carbon ?Black|Sophos|McAfee|Trellix|Symantec|ESET|Tanium|Qualys|BigFix|Rapid7|Wazuh|Elastic|Endpoint|Antivirus|EDR|Threat'
try {
    foreach ($s in @(Get-CimInstance Win32_Service -ErrorAction Stop)) {
        if (($s.DisplayName -match $pattern) -or ($s.Name -match $pattern)) {
            Say ("  {0,-28} {1,-8} {2}" -f $s.Name, $s.State, $s.DisplayName)
        }
    }
} catch { Say '  services unreadable' 'Yellow' }

# ------------------------------------------------------------ 4. Defender
Section '4. Microsoft Defender'
try {
    $st = Get-MpComputerStatus -ErrorAction Stop
    Say "Mode: $($st.AMRunningMode)   real-time: $($st.RealTimeProtectionEnabled)   tamper protection: $($st.IsTamperProtected)   engine: $($st.AMProductVersion)"
} catch { Say 'Get-MpComputerStatus unavailable (Defender off, replaced by another product, or no module)' 'Yellow' }
$asrNames = @{
    'D3E037E1-3EB8-44C8-A917-57927947596D' = 'Block JavaScript or VBScript from launching downloaded executable content (relevant: the watcher starts through a VBScript)'
    '5BEB7EFE-FD9A-4556-801D-275E5FFC04CC' = 'Block execution of potentially obfuscated scripts (relevant: scripts)'
}
try {
    $pref = Get-MpPreference -ErrorAction Stop
    $ids = @($pref.AttackSurfaceReductionRules_Ids)
    $acts = @($pref.AttackSurfaceReductionRules_Actions)
    if ($ids.Count -eq 0 -or ($ids.Count -eq 1 -and -not $ids[0])) { Say 'Attack-surface-reduction rules: none configured' }
    else {
        Say 'Attack-surface-reduction rules (action: 0 off, 1 BLOCK, 2 audit, 6 warn):'
        for ($i = 0; $i -lt $ids.Count; $i++) {
            $g = ([string]$ids[$i]).ToUpper()
            $a = ''
            if ($i -lt $acts.Count) { $a = [string]$acts[$i] }
            $name = ''
            if ($asrNames.ContainsKey($g)) { $name = $asrNames[$g] }
            Say ("  {0}  action {1}  {2}" -f $g, $a, $name)
        }
    }
    $ex = @($pref.ExclusionPath)
    if ($ex.Count -gt 0 -and $ex[0]) {
        foreach ($e in $ex) { Say "  exclusion path: $e" }
    }
} catch { Say 'Get-MpPreference unavailable' 'Yellow' }
Say 'Defender detections and ASR blocks/audits in the last 30 days (events 1116/1117/1121/1122):'
try {
    $flt = @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; Id = @(1116, 1117, 1121, 1122); StartTime = (Get-Date).AddDays(-30) }
    $ev = @(Get-WinEvent -FilterHashtable $flt -MaxEvents 25 -ErrorAction Stop)
    foreach ($e in $ev) { Say ("  {0:yyyy-MM-dd HH:mm}  id {1}  {2}" -f $e.TimeCreated, $e.Id, (Short $e.Message 260)) 'Yellow' }
} catch { Say '  none (or the Defender log is not readable)' }

# ------------------------------------------------------- 5. app control
Section '5. App control (AppLocker / WDAC)'
try {
    $x = [xml](Get-AppLockerPolicy -Effective -Xml -ErrorAction Stop)
    $rcs = @($x.AppLockerPolicy.RuleCollection)
    if ($rcs.Count -eq 0) { Say 'AppLocker: no effective policy' }
    foreach ($rc in $rcs) {
        $nr = @($rc.ChildNodes).Count
        Say ("AppLocker {0,-8} enforcement: {1}   rules: {2}" -f $rc.Type, $rc.EnforcementMode, $nr)
    }
} catch { Say 'AppLocker: not available or no policy' }
try {
    $dg = Get-CimInstance -Namespace 'root/Microsoft/Windows/DeviceGuard' -ClassName Win32_DeviceGuard -ErrorAction Stop
    Say "WDAC kernel enforcement: $($dg.CodeIntegrityPolicyEnforcementStatus)   user-mode enforcement: $($dg.UsermodeCodeIntegrityPolicyEnforcementStatus)   (0 off, 1 audit, 2 enforced)"
} catch { Say 'WDAC / Device Guard: not reported' }

# ---------------------------------------------------------- 6. PowerShell
Section '6. PowerShell policy and logging'
foreach ($ep in @(Get-ExecutionPolicy -List)) { Say ("ExecutionPolicy {0,-14} {1}" -f $ep.Scope, $ep.ExecutionPolicy) }
Say "Language mode: $($ExecutionContext.SessionState.LanguageMode)   (ConstrainedLanguage would break the watcher)"
$psPol = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell'
foreach ($sub in @('ScriptBlockLogging', 'ModuleLogging', 'Transcription')) {
    $kp = Join-Path $psPol $sub
    if (Test-Path $kp) {
        $vals = Get-ItemProperty $kp -ErrorAction SilentlyContinue
        Say ("{0} policy: {1}" -f $sub, (Short ($vals | Out-String) 200)) 'Yellow'
    } else { Say "$sub policy: not set" }
}

# ---------------------------------------------------- 7. Windows Script Host
Section '7. VBScript / Windows Script Host'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$vbsdll = Join-Path $env:SystemRoot 'System32\vbscript.dll'
Say "wscript.exe present: $(Test-Path $wscript)   vbscript.dll present: $(Test-Path $vbsdll)"
foreach ($root in @('HKLM', 'HKCU')) {
    $kp = "$($root):\SOFTWARE\Microsoft\Windows Script Host\Settings"
    $v = $null
    if (Test-Path $kp) { $v = (Get-ItemProperty $kp -ErrorAction SilentlyContinue).Enabled }
    if ($null -eq $v) { Say "$root Script Host 'Enabled': not set (default: enabled)" }
    else { Say "$root Script Host 'Enabled': $v   (0 = disabled)" 'Yellow' }
}

# ------------------------------------------------- 8. STAN tasks already here
Section '8. STAN scheduled tasks already on this PC (same hidden-PowerShell pattern)'
foreach ($tn in @('STAN Evosep log mirror', 'STAN Bruker backup mirror', 'STAN Flinders Copy', 'STAN HyStar watcher')) {
    $t = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
    if (-not $t) { Say "$tn : not installed" ; continue }
    $info = Get-ScheduledTaskInfo -TaskName $tn -ErrorAction SilentlyContinue
    $act = @($t.Actions)[0]
    Say ("{0} : state {1}, last run {2}, last result {3} (0 = OK), next {4}" -f $tn, $t.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
    Say ("    runs: {0} {1}" -f $act.Execute, (Short $act.Arguments 160))
}

# ----------------------------------------------------------- 9. live test
if ($TryLaunch) {
    Section '9. Live test: one hidden PowerShell started through a VBScript'
    $dir = Join-Path $env:TEMP "stan_launch_test_$stamp"
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $marker = Join-Path $dir 'marker.txt'
    $ps1 = Join-Path $dir 'test.ps1'
    $vbs = Join-Path $dir 'test.vbs'
    Set-Content -LiteralPath $ps1 -Value 'Set-Content -LiteralPath (Join-Path $PSScriptRoot ''marker.txt'') -Value ok' -Encoding ASCII
    $cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"`"$ps1`"`""
    $line = "WScript.Quit CreateObject(`"WScript.Shell`").Run(`"$cmd`", 0, True)"
    Set-Content -LiteralPath $vbs -Value $line -Encoding ASCII
    $started = Get-Date
    try {
        $p = Start-Process -FilePath $wscript -ArgumentList @('//B', '//Nologo', "`"$vbs`"") -PassThru -ErrorAction Stop
        $done = $p.WaitForExit(30000)
        $code = 'n/a'
        if ($done) { $code = $p.ExitCode }
        Say "wscript finished within 30 s: $done   exit code: $code"
    } catch { Say "wscript could not be started: $($_.Exception.Message)" 'Red' }
    if (Test-Path $marker) { Say 'RESULT: the hidden PowerShell RAN (marker written).' 'Green' }
    else { Say 'RESULT: the hidden PowerShell did NOT run -- something blocked it.' 'Red' }
    Start-Sleep -Seconds 5
    try {
        $flt = @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; Id = @(1116, 1117, 1121, 1122); StartTime = $started.AddSeconds(-5) }
        $ev = @(Get-WinEvent -FilterHashtable $flt -MaxEvents 10 -ErrorAction Stop)
        foreach ($e in $ev) { Say ("Defender event {0}: {1}" -f $e.Id, (Short $e.Message 300)) 'Red' }
    } catch { Say 'No Defender detection or ASR event during the test.' 'Green' }
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    Say 'An EDR agent (section 3) may report this test to its console without blocking it; only whoever manages that agent can say.'
} else {
    Section '9. Live test: not run (add /trylaunch to the .bat, or -TryLaunch, to run it)'
}

# --------------------------------------------------------------- 10. save
$outDir = 'Y:\brett\security_check'
if (-not (Test-Path 'Y:\brett')) { $outDir = '\\128.120.208.42\proteomics-grp\brett\security_check' }
try {
    New-Item -ItemType Directory -Path $outDir -Force -ErrorAction Stop | Out-Null
    $out = Join-Path $outDir ('{0}_{1}.txt' -f $env:COMPUTERNAME, $stamp)
    [IO.File]::WriteAllLines($out, $script:Report.ToArray(), (New-Object Text.UTF8Encoding $false))
    Say ''
    Say "Report saved: $out" 'Green'
} catch {
    Say ''
    Say "Could not save the report to $outDir ($($_.Exception.Message)). Copy this window instead." 'Red'
}
