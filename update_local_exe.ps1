<#
.SYNOPSIS
    Her release sonrasi yereldeki SistemMonitor exe'lerini temizler ve en guncel exe'yi kurar.
.DESCRIPTION
    - Calisan SistemMonitor surecini durdurur (lock'u birakir).
    - En guncel exe'yi repo'dan (origin/main, release workflow tarafindan otomatik commit edilir)
      veya GitHub release'ten alir.
    - Hedef klasorlerdeki tum SistemMonitor*.exe dosyalarini siler, yerine SistemMonitor.exe'yi koyar.
#>
$ErrorActionPreference = "Stop"

$base   = $PSScriptRoot                                   # repo koku: .../SistemMonitor/SistemMonitor
$parent = Split-Path $base -Parent                        # masaustu klasoru: .../SistemMonitor
$tmp    = Join-Path $env:TEMP "SistemMonitor_update"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$src    = Join-Path $tmp "SistemMonitor.exe"

# 1) En guncel exe'yi al (oncelik: repo origin/main, fallback: GitHub release)
git -C $base fetch origin 2>&1 | Out-Null
$got = $false
try {
    cmd /c "git -C $base show origin/main:SistemMonitor.exe > $src"
    if ((Test-Path $src) -and ((Get-Item $src).Length -gt 1MB)) {
        Write-Host "Exe repo'dan alindi (origin/main)."
        $got = $true
    }
} catch { $got = $false }

if (-not $got) {
    Write-Host "Repo'dan alinamadi, GitHub release deneniyor..."
    $tag = (gh release view --repo Bedrettin1/SistemMonitor --json tagName 2>$null | ConvertFrom-Json).tagName
    gh release download $tag --repo Bedrettin1/SistemMonitor -p "SistemMonitor.exe" --clobber --dir $tmp 2>&1
}

if (-not (Test-Path $src)) { Write-Error "Yeni exe temin edilemedi."; exit 1 }

# 2) Calisan sureci durdur (lock'u birak)
Get-Process -Name "SistemMonitor" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# 3) Eski exeleri kaldir, yenisini kur
foreach ($dir in @($base, $parent)) {
    Get-ChildItem -Path $dir -Filter "SistemMonitor*.exe" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Copy-Item $src -Destination (Join-Path $dir "SistemMonitor.exe") -Force
    Write-Host "Guncellendi: $(Join-Path $dir 'SistemMonitor.exe')  ($([math]::Round((Get-Item (Join-Path $dir 'SistemMonitor.exe')).Length/1MB,1)) MB)"
}

# 4) Yerel main'i origin/main ile hizala (exe commit'ini da al) - best-effort
try { git -C $base pull --ff-only origin main 2>&1 | Out-Null } catch { Write-Host "Pull atlandi (zaten guncel veya yerel degisiklik var)." }

# 5) Yeni exe'yi yonetici olarak baslat (CPU Package sicakligi icin gerekli)
Start-Process -FilePath (Join-Path $base "SistemMonitor.exe") -Verb RunAs
Write-Host "Tamamlandi. SistemMonitor.exe en guncel surumde ve yonetici olarak baslatildi (CPU Package sicakligi icin)."
