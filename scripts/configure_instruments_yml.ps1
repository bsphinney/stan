# configure_instruments_yml.ps1
#
# Writes ~/.stan/instruments.yml and ~/.stan/community.yml for one instrument.
# Called by install_stan_lumosrox.bat and install_stan_tims10878.bat.
#
# Parameters (all mandatory):
#   -InstrumentName  "Orbitrap Fusion Lumos" | "timsTOF HT"
#   -Family          Lumos | timsTOF
#   -Vendor          thermo | bruker
#   -WatchDir        E:\Data | D:\Data
#   -Extensions      .raw | .d
#   -StableSecs      30 | 60
#   -HiveUploadDir   Y:\STAN\incoming\lumosRox  (Windows SMB path)
#   -HiveUser        brettsp
#   -HiveHost        hive.hpc.ucdavis.edu
#   -HiveVenv        /quobyte/proteomics-grp/brett/stan_venv
#   -HiveDispatchYml /quobyte/proteomics-grp/STAN/dispatch.yml
#
# CLAUDE.md PS5.1 rules observed:
#   - No + string concatenation; all string building via interpolation
#   - No inline ternary if
#   - No Where-Object pipelines
#   - Full-file rewrite on every edit

param(
    [Parameter(Mandatory=$true)]  [string] $InstrumentName,
    [Parameter(Mandatory=$true)]  [string] $Family,
    [Parameter(Mandatory=$true)]  [string] $Vendor,
    [Parameter(Mandatory=$true)]  [string] $WatchDir,
    [Parameter(Mandatory=$true)]  [string] $Extensions,
    [Parameter(Mandatory=$true)]  [int]    $StableSecs,
    [Parameter(Mandatory=$true)]  [string] $HiveUploadDir,
    [Parameter(Mandatory=$true)]  [string] $HiveUser,
    [Parameter(Mandatory=$true)]  [string] $HiveHost,
    [Parameter(Mandatory=$true)]  [string] $HiveVenv,
    [Parameter(Mandatory=$true)]  [string] $HiveDispatchYml
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$stanDir = "$env:USERPROFILE\.stan"
if (-not (Test-Path $stanDir)) {
    New-Item -ItemType Directory -Path $stanDir -Force | Out-Null
}

# ------------------------------------------------------------------
# instruments.yml
# ------------------------------------------------------------------
$instrumentsYml = "$stanDir\instruments.yml"

$instrumentsContent = @"
instruments:
- name: "$InstrumentName"
  vendor: $Vendor
  watch_dir: "$WatchDir"
  extensions:
  - $Extensions
  stable_secs: $StableSecs
  qc_only: true
  enabled: true
  hela_amount_ng: 50.0
  community_submit: true
  column_vendor: PepSep
  column_model: ""
  monitor_all_files: true
  exclude_pattern: '(?i)(wash|blank|blnk|blk)'
  aliases:
  - auto
  - unknown
  processing_mode: hive
  hive_host: $HiveHost
  hive_user: $HiveUser
  hive_venv: $HiveVenv
  hive_dispatch_yml: $HiveDispatchYml
  hive_upload_dir: "$HiveUploadDir"
  submit_after_upload: true
"@

Set-Content -Path $instrumentsYml -Value $instrumentsContent -Encoding UTF8
Write-Host "  Wrote $instrumentsYml" -ForegroundColor Green

# ------------------------------------------------------------------
# community.yml
# ------------------------------------------------------------------
$communityYml = "$stanDir\community.yml"

if (Test-Path $communityYml) {
    Write-Host "  community.yml already exists — leaving it untouched." -ForegroundColor Yellow
    Write-Host "  (Edit $communityYml to add your auth_token and display_name.)" -ForegroundColor Yellow
} else {
    $communityContent = @"
# STAN community benchmark config.
# Fill in auth_token (get it from Brett) and pick a display_name for the
# leaderboard.  display_name is public; keep it short (<=40 chars).
display_name: "UC Davis Proteomics Core"
auth_token: ""
error_telemetry: true
community_submit: true
email_reports:
  enabled: false
  to: ""
  daily: "07:00"
  weekly: monday
"@
    Set-Content -Path $communityYml -Value $communityContent -Encoding UTF8
    Write-Host "  Wrote $communityYml" -ForegroundColor Green
    Write-Host "  ACTION REQUIRED: open $communityYml and paste your auth_token." -ForegroundColor Yellow
}
