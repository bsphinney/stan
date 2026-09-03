<#
.SYNOPSIS
    Read-only survey + dump of the Bruker HyStar PostgreSQL database on a
    timsTOF, written straight to the Quobyte share.

.DESCRIPTION
    Answers one question: does Bruker's database hold the sample table (the
    acquisition queue)? If it does, the queue reconciliation that currently
    depends on someone exporting an .xlsx by hand becomes automatic.

    SAFETY. This instrument's database is live while it acquires, so the script
    is read-only by construction:

      * it never writes to the database, never stops the service, and never
        touches a file inside D:\BrukerDBData;
      * it uses pg_dump, which takes an MVCC snapshot and holds no blocking
        lock, rather than copying the data directory. A file copy of a running
        PGDATA is a torn snapshot -- files change under the copy and the result
        may not restore at all;
      * the schema pass is kilobytes and finishes in about a second. Table DATA
        is dumped only for tables whose names look like a sample table, and
        only when -IncludeData is passed.

    Everything lands in a timestamped folder on the share, so repeated runs
    never overwrite each other.

.EXAMPLE
    .\dump_bruker_db.ps1
    Survey + schema only. Safe to run during an acquisition.

.EXAMPLE
    .\dump_bruker_db.ps1 -IncludeData
    Also dumps rows from sample-table-shaped tables. Prefer to run this when
    the instrument is idle.
#>
[CmdletBinding()]
param(
    # Where PGDATA lives; only postgresql.conf and PG_VERSION are read from it.
    [string] $PgData = 'D:\BrukerDBData',

    # Quobyte share. Y: is proteomics-grp (\\128.120.208.42) on this instrument;
    # the UNC fallback is used when the drive letter is not mapped in this session.
    [string] $OutRoot = 'Y:\brett\bruker_db',
    [string] $OutRootUnc = '\\128.120.208.42\proteomics-grp\brett\bruker_db',

    [string] $PgUser = 'postgres',
    [int]    $Port = 0,          # 0 = read it out of postgresql.conf
    [switch] $IncludeData,       # also dump rows from sample-table-shaped tables
    [int]    $MaxDataMB = 200,   # refuse a single table dump larger than this

    # Server logs from $PgData\log. Newest first, stopping at the size cap, so
    # a long-running instance cannot fill the share.
    [int]    $LogDays = 30,
    [int]    $MaxLogMB = 500
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$host_ = $env:COMPUTERNAME

# Tables worth the data pass. Deliberately narrow: we want the queue, not
# Bruker's instrument telemetry, which is large and changes constantly.
$DataPatterns = @('sample', 'batch', 'acquisi', 'queue', 'worklist', 'vial', 'tray')

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }

# -- output folder ------------------------------------------------------------
$root = $OutRoot
try { if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null } }
catch { Say "  $OutRoot unavailable, falling back to UNC" 'Yellow'; $root = $OutRootUnc }
$out = Join-Path $root "$host_`_$stamp"
New-Item -ItemType Directory -Path $out -Force | Out-Null
$log = Join-Path $out 'dump.log'
function Log($m) { $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
                   Add-Content -Path $log -Value $line; Say $line }

Log "host=$host_  pgdata=$PgData  out=$out"

# -- locate the binaries ------------------------------------------------------
$pgDump = $null; $psql = $null
$search = @("$env:ProgramFiles", "${env:ProgramFiles(x86)}", 'C:\Program Files', 'D:\')
foreach ($base in ($search | Select-Object -Unique)) {
    if (-not (Test-Path $base)) { continue }
    if (-not $pgDump) {
        $pgDump = Get-ChildItem $base -Filter pg_dump.exe -Recurse -ErrorAction SilentlyContinue |
                  Select-Object -First 1 -ExpandProperty FullName
    }
    if ($pgDump) { break }
}
if (-not $pgDump) { Log 'FATAL: pg_dump.exe not found. Is PostgreSQL installed with HyStar?'; exit 1 }
$psql = Join-Path (Split-Path $pgDump) 'psql.exe'
Log "pg_dump = $pgDump"
if (-not (Test-Path $psql)) { Log 'FATAL: psql.exe not beside pg_dump.exe'; exit 1 }

# -- port + version, straight from PGDATA (read-only) -------------------------
if ($Port -eq 0) {
    $Port = 5432
    $conf = Join-Path $PgData 'postgresql.conf'
    if (Test-Path $conf) {
        $m = Select-String -Path $conf -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value }
    }
}
$pgver = ''
$vf = Join-Path $PgData 'PG_VERSION'
if (Test-Path $vf) { $pgver = (Get-Content $vf -Raw).Trim() }
Log "port=$Port  PG_VERSION=$pgver"
Copy-Item (Join-Path $PgData 'postgresql.conf') (Join-Path $out 'postgresql.conf.copy') -EA SilentlyContinue

# -- server logs --------------------------------------------------------------
# The one place an acquisition failure leaves a trace that the raw files do
# not: two wells on plate S5 (F6, H6) wrote a method folder and metadata but
# no analysis.tdf, and nothing in the .d says why. Copying, not moving --
# PostgreSQL keeps writing to the current file, and reading it is harmless
# (a copy of an actively-appended log just ends early).
$srcLog = Join-Path $PgData 'log'
if (Test-Path $srcLog) {
    $dstLog = Join-Path $out 'log'
    New-Item -ItemType Directory -Path $dstLog -Force | Out-Null
    $cut = (Get-Date).AddDays(-$LogDays)
    $files = Get-ChildItem $srcLog -File -Recurse -ErrorAction SilentlyContinue |
             Where-Object { $_.LastWriteTime -gt $cut } |
             Sort-Object LastWriteTime -Descending
    $taken = 0; $bytes = 0L
    foreach ($f in $files) {
        if (($bytes + $f.Length) -gt ($MaxLogMB * 1MB)) {
            Log "  log cap ${MaxLogMB}MB reached, stopping at $taken files"; break
        }
        # -Force so a file the server currently holds open is still readable.
        Copy-Item $f.FullName (Join-Path $dstLog $f.Name) -Force -EA SilentlyContinue
        $taken++; $bytes += $f.Length
    }
    Log ("  logs      -> log\  ({0} files, {1:N1} MB, last {2}d)" -f $taken, ($bytes / 1MB), $LogDays)
    Copy-Item (Join-Path $PgData 'current_logfiles') (Join-Path $out 'current_logfiles.copy') -EA SilentlyContinue
} else {
    Log "  no log directory at $srcLog (logging_collector may be off; check postgresql.conf)"
}

# -- connect ------------------------------------------------------------------
$env:PGCLIENTENCODING = 'UTF8'
$conn = @('-h', '127.0.0.1', '-p', "$Port", '-U', $PgUser)

$dbs = & $psql @conn -Atc "SELECT datname FROM pg_database WHERE datistemplate=false;" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "FATAL: cannot connect as '$PgUser' on port $Port."
    Log ($dbs | Out-String)
    Log "If it asked for a password, set `$env:PGPASSWORD before running, or check"
    Log "pg_hba.conf in $PgData for the local auth method."
    exit 1
}
$dbs = $dbs | Where-Object { $_ -and $_.Trim() }
Log ("databases: " + ($dbs -join ', '))
Set-Content (Join-Path $out 'databases.txt') $dbs

# -- per database: inventory + schema (+ optional data) -----------------------
foreach ($db in $dbs) {
    $db = $db.Trim()
    if ($db -in @('postgres')) { continue }
    Log "--- $db ---"

    # Table inventory with row estimates and on-disk size. Uses the planner's
    # reltuples, so it costs nothing and never scans a table.
    $inv = @"
SELECT n.nspname AS schema, c.relname AS table,
       c.reltuples::bigint AS est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')
 ORDER BY pg_total_relation_size(c.oid) DESC;
"@
    & $psql @conn -d $db -c $inv > (Join-Path $out "$db.tables.txt") 2>&1
    Log "  inventory -> $db.tables.txt"

    # Columns, so a sample table is recognisable even if oddly named.
    $cols = @"
SELECT table_schema||'.'||table_name AS tbl, column_name, data_type
  FROM information_schema.columns
 WHERE table_schema NOT IN ('pg_catalog','information_schema')
 ORDER BY 1, ordinal_position;
"@
    & $psql @conn -d $db -c $cols > (Join-Path $out "$db.columns.txt") 2>&1

    # Schema. Tiny, and the thing that actually answers the question.
    & $pgDump @conn -d $db -s --no-owner --no-privileges `
        -f (Join-Path $out "$db.schema.sql") 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Log "  schema    -> $db.schema.sql" }
    else { Log "  schema dump FAILED for $db" }

    if (-not $IncludeData) { continue }

    # Data, only for sample-table-shaped tables, only under the size cap.
    $like = ($DataPatterns | ForEach-Object { "c.relname ILIKE '%$_%'" }) -join ' OR '
    $q = @"
SELECT n.nspname||'.'||c.relname
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')
   AND ($like)
   AND pg_total_relation_size(c.oid) < $($MaxDataMB * 1MB);
"@
    $tables = & $psql @conn -d $db -Atc $q 2>&1 | Where-Object { $_ -and $_.Trim() }
    if (-not $tables) { Log "  no sample-table-shaped tables in $db"; continue }
    Log ("  data tables: " + ($tables -join ', '))
    $args = @()
    foreach ($t in $tables) { $args += @('-t', $t.Trim()) }
    & $pgDump @conn -d $db --data-only --no-owner --no-privileges @args `
        -f (Join-Path $out "$db.sampletables.sql") 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Log "  data      -> $db.sampletables.sql" }
    else { Log "  data dump FAILED for $db" }
}

# -- done ---------------------------------------------------------------------
$size = (Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum
Log ("done. {0} files, {1:N1} MB" -f (Get-ChildItem $out -Recurse -File).Count, ($size / 1MB))
Say ''
Say "Wrote: $out" 'Green'
Say "On Hive this is: /quobyte/proteomics-grp/brett/bruker_db/$host_`_$stamp" 'Green'
