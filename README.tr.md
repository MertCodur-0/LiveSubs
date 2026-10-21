# LiveSubs

> Windows'ta herhangi bir oyunun İngilizce altyazısını gerçek zamanlı ve tamamen yerel olarak Türkçeye çevirir.

[English](README.md)

LiveSubs bir oyun penceresinin, monitörün ya da capture kartının altyazı bölgesini Windows'un
yerleşik OCR'ı ile okur, her satırı [Ollama](https://ollama.com) üzerinden yerel bir dil modeliyle
çevirir ve çeviriyi oyunun üstünde, tıklamaların içinden geçtiği bir overlay'de gösterir. Bulut API
yok; her şey kendi bilgisayarında çalışır.

**Durum:** geliştirme aşamasında. Mimari ve yol haritası için [CLAUDE.md](CLAUDE.md).

## Planlanan özellikler

- Pencere, monitör veya capture kartı kaynağı; oyun başına profil
- Parlak, hareketli oyun arka planını yok sayan altyazı rengi maskeleme
- GPU'da hızlı çeviri, isteğe bağlı CPU'da daha kaliteli ikinci geçiş
- Uygulama içinden Ollama model yönetimi (indirme, VRAM uygunluğu, yan yana test)
- Ekran yakalamadan gizlenen overlay; OCR kendi çevirisini okumaz
- İsteğe bağlı: aynı Wi-Fi'daki telefonda QR kodla görüntüleme

## Geliştirme

Windows 10 2004+ / Windows 11 ve **Python 3.13** gerekir.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m livesubs
.\.venv\Scripts\python.exe -m pytest
```

## Lisans

[MIT](LICENSE)
