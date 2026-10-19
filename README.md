# Dikte — telefondan konuş, PC'de yazsın

iPhone'da konuşun; metin Windows PC'nizde **imlecin olduğu yere** yazılsın. Türkçe öncelikli, konuşma tanıma **tamamen yerel** (Whisper).

*Speak on your iPhone, text is typed at the cursor on your Windows PC. Turkish-first, fully local speech-to-text (whisper.cpp / faster-whisper).*

```
iPhone Safari (PWA) ── HTTPS + token ──▶ FastAPI sunucusu (PC, :8443)
                                           ffmpeg → 16 kHz WAV → whisper.cpp (Vulkan GPU) / faster-whisper (CPU)
                                           → SendInput ile odaktaki pencereye yazar
```

## Özellikler

- Yerel STT: whisper.cpp (Vulkan; AMD/Intel/NVIDIA GPU'larda çalışır) veya CPU yedeği faster-whisper
- 11,5 sn Türkçe ses ≈ 0,9 sn (RX 6600, large-v3-turbo q8_0)
- Odaktaki pencereye Unicode olarak yazma (Türkçe karakterler, emoji) veya pano + Ctrl+V
- Geri al (son dikte / bu penceredeki tüm dikteler), Enter/Tab/Boşluk/Backspace tuşları
- İsteğe bağlı **Temizle / Kısalt** modları (Google Gemini). Başarısız olursa ham metin yazılır; dikte hiç beklemez
- Whisper'ın Türkçe "Altyazı M.K.", "Abone olmayı unutmayın" gibi halüsinasyonlarını süzer
- Ana ekrana eklenebilir PWA

## Gereksinimler

- Windows 10/11, [uv](https://docs.astral.sh/uv/), [ffmpeg](https://ffmpeg.org/) (PATH'te)
- GPU için: VS 2022 Build Tools (C++), CMake, Vulkan SDK — whisper.cpp'yi derlemek için
- iPhone ve PC aynı yerel ağda

## Kurulum

```powershell
uv sync                                    # bağımlılıklar (Python 3.12'yi uv kurar)
scripts\build_whispercpp.ps1               # whisper.cpp + Vulkan → bin\whisper-server.exe
scripts\download_model.ps1                 # models\ggml-large-v3-turbo-q8_0.bin
scripts\firewall.ps1                       # (yönetici) 8443/8080 portlarını yalnızca Özel ağda açar
copy .env.example .env                     # isteğe bağlı: GEMINI_API_KEY
uv run python -m server.main               # veya start.bat
```

GPU derlemek istemiyorsanız: `uv sync --extra cpu` ve `uv run python -m server.main --backend faster-whisper` (yavaştır).

### iPhone

1. Konsolda yazan **kurulum adresini** (`http://<PC-IP>:8080/`) Safari'de açın, yerel CA sertifikasını yükleyip **tam güven** verin.
2. PC ekranındaki **QR kodu** okutun (token'lı uygulama adresi).
3. **Paylaş › Ana Ekrana Ekle.**

Tüm ayarlar `config.toml` içindedir.

## Güvenlik

Bu uygulama ağdaki bir cihazın bilgisayarınızda **klavye girişi** yapmasına izin verir. Token kimlik doğrulaması, yalnızca yerel/özel ağ adlarıyla sınırlandırılmış (NameConstraints) yerel CA, yalnızca Özel ağ profilinde açılan güvenlik duvarı kuralları ve boyut sınırları vardır. Ayrıntılar ve yapmanız gerekenler: [SECURITY.md](SECURITY.md).

`data/` (CA özel anahtarı, token) ve `.env` asla paylaşılmamalıdır; `.gitignore` bunları hariç tutar.

## Geliştirme

```powershell
uv run pytest                                      # testler (STT, Gemini ve SendInput sahte)
uv run python -m server.stt ses.m4a                # dosya çözümle, süreleri yazdır
uv run python -m server.refine --models            # anahtarın gördüğü Gemini modelleri
```

## Lisans

[MIT](LICENSE)
