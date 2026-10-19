# Güvenlik

Bu sunucu, ağdaki bir cihazın **bilgisayarınızda klavye girişi yapmasına** izin verir. Bunu aklınızda tutun.

## Tasarımdaki önlemler

- **Token kimlik doğrulaması:** Tüm `/api/*` uçları `x-token` başlığı ister (128 bit rastgele, `data/token.txt`, sabit zamanlı karşılaştırma). Token yalnızca URL'nin `#` kısmında taşınır; hash sunucuya ya da Referer'a gönderilmez.
- **HTTPS:** Uygulama yalnızca TLS üzerinden sunulur. Sertifikalar ilk çalıştırmada makinenizde üretilen **yerel bir kök CA** ile imzalanır; depoda hiçbir sertifika veya anahtar yoktur.
- **HTTP portu (8080)** yalnızca kurulum sayfasını ve CA'nın *açık* sertifikasını sunar; API'ye erişim vermez.
- **Güvenlik duvarı:** `scripts/firewall.ps1` portları yalnızca **Özel** ağ profilinde açar. Kafe/otel gibi Genel ağlarda dışarıya kapalı kalır.
- **Boyut sınırları:** Ses yüklemesi 50 MB, yazdırılacak metin 20 000 karakterle sınırlıdır.
- API dokümantasyon uçları (`/docs`, `/openapi.json`) kapalıdır.
- Ses yalnızca yerelde (whisper.cpp / faster-whisper) işlenir. İsteğe bağlı Temizle/Kısalt modları **metni** Google Gemini'ye gönderir; Ham modda hiçbir şey dışarı çıkmaz.

## Sizin yapmanız gerekenler

- `data/` klasörünü (özellikle `certs/ca.key` ve `token.txt`) ve `.env` dosyasını **kimseyle paylaşmayın, commit'lemeyin** (`.gitignore` zaten hariç tutar).
- Token sızdıysa `data/token.txt` dosyasını silip sunucuyu yeniden başlatın; yeni QR kodunu okutun.
- CA anahtarı sızdıysa `data/certs/` klasörünü silin, iPhone'dan eski profili kaldırın ve kurulumu yeniden yapın — aksi halde o CA ile herhangi bir site için sahte sertifika üretilebilir.
- Portları yönlendirici üzerinden internete açmayın (port forwarding yapmayın).
- Sunucuyu yönetici olarak çalıştırmayın; gerek yoksa yükseltilmiş pencerelere yazamaması bir güvenlik özelliğidir (UIPI).

## Açık bildirme

Bir güvenlik açığı bulursanız lütfen herkese açık issue yerine GitHub'ın **Security → Report a vulnerability** (özel bildirim) özelliğini kullanın.
