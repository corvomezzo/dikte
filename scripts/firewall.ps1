# Telefondan erişim için 8443 (uygulama) ve 8080 (kurulum) portlarını açar.
# Yönetici PowerShell'de çalıştırın:
#   scripts\firewall.ps1             -> kural yalnızca Özel ağ profilinde geçerli
#   scripts\firewall.ps1 -SetPrivate -> ayrıca bağlı ağın profilini Genel'den Özel'e çevirir
param([switch]$SetPrivate)
$ErrorActionPreference = "Stop"

if ($SetPrivate) {
    Get-NetConnectionProfile | Where-Object NetworkCategory -eq "Public" | ForEach-Object {
        Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private
        Write-Host "Özel yapıldı: $($_.InterfaceAlias) ($($_.Name))"
    }
}

foreach ($p in 8443, 8080) {
    $name = "Dikte TCP $p"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow -Profile Private | Out-Null
    }
    Write-Host "Açık: $name (Özel ağ)"
}

if ($SetPrivate) {
    # Ağ Genel'den Özel'e geçince, Windows'un yalnızca Genel profil için izin verdiği uygulamalar
    # (ör. LocalSend) gelen bağlantı alamaz hale gelir. Otomatik değiştirmiyoruz; listeleyip uyarıyoruz.
    # Windows'un kendi kurallarının çoğu Özel ağ için aynı adlı ayrı bir eşe sahiptir; onları sayma.
    $allow = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True
    $coversPrivate = $allow | Where-Object { $_.Profile -eq "Any" -or "$($_.Profile)" -match "Private" } |
        Select-Object -ExpandProperty DisplayName -Unique
    $onlyPublic = $allow | Where-Object { $_.Profile -eq "Public" -and $coversPrivate -notcontains $_.DisplayName } |
        Select-Object -ExpandProperty DisplayName -Unique
    if ($onlyPublic) {
        Write-Warning "Şu uygulamalara yalnızca Genel ağda izin verilmiş; Özel ağda engellenecekler:"
        $onlyPublic | ForEach-Object { Write-Host "  - $_" }
        Write-Host 'Özel ağda da izin vermek için: Set-NetFirewallRule -DisplayName "<ad>" -Profile Public,Private'
    }
}

$public = Get-NetConnectionProfile | Where-Object NetworkCategory -eq "Public"
if ($public) {
    Write-Warning "Şu ağ(lar) hâlâ 'Genel': $($public.InterfaceAlias -join ', '). Telefon bağlanamaz; -SetPrivate ile çalıştırın."
}
