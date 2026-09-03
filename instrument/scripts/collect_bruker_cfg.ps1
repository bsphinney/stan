# Copy Bruker Compass Server's small config files to the share so the
# datasource credential can be located precisely. Read-only: it copies
# config, changes nothing. The etc\ folder is Karaf .cfg + .xml text files,
# a few hundred KB in total.
$ErrorActionPreference = 'SilentlyContinue'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$root = 'Y:\brett\bruker_db'
if (-not (Test-Path $root)) { $root = '\\128.120.208.42\proteomics-grp\brett\bruker_db' }
$out = Join-Path $root "compass_cfg_$($env:COMPUTERNAME)_$stamp"
New-Item -ItemType Directory -Path $out -Force | Out-Null

$bases = @('C:\Program Files\Bruker\Bruker Compass Server',
           'C:\Program Files (x86)\Bruker\Bruker Compass Server')
foreach ($b in $bases) {
    if (-not (Test-Path $b)) { continue }
    foreach ($sub in @('etc', 'config', 'conf')) {
        $src = Join-Path $b $sub
        if (Test-Path $src) {
            # only small text config, never data or jars
            Get-ChildItem $src -Recurse -File -Include *.cfg,*.xml,*.properties,*.conf,*.ini -EA SilentlyContinue |
              Where-Object { $_.Length -lt 512KB } | ForEach-Object {
                $rel = $_.FullName.Substring($b.Length).TrimStart('\')
                $dst = Join-Path $out $rel
                New-Item -ItemType Directory -Path (Split-Path $dst) -Force | Out-Null
                Copy-Item $_.FullName $dst -Force
              }
        }
    }
}
$n = (Get-ChildItem $out -Recurse -File).Count
Write-Host ''
Write-Host "Copied $n config files to:" -ForegroundColor Green
Write-Host "   $out"
Write-Host "On Hive: /quobyte/proteomics-grp/brett/bruker_db/$(Split-Path $out -Leaf)" -ForegroundColor Cyan
Write-Host ''
Write-Host 'Press any key to continue . . .'
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
