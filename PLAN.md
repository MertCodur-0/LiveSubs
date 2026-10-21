# LiveSubs — Mimari ve Uygulama Planı (onaylı)

Bu plan önceki Claude Code oturumunda çıkarıldı ve onaylandı. En alttaki "Onaylanan düzeltme ve eklemeler" bölümü planın üstüne geçerlidir; çelişki varsa o bölüm kazanır.

## Ortam notları
- Python 3.13 kurulu, ama `py` varsayılan olarak 3.15'i açıyor; sanal ortam 3.13 ile açıkça kurulacak.
- Ollama kurulu ama çalışmıyor olabilir; açılış kontrolü bunu yakalayacak.
- Git kurulu değildi (gh var). Kurulması gerekiyor (aşağıya bak).
- Proje klasörü: `C:\Users\mertc\Projects\LiveSubs`

## 1. Genel mimari
İki process: arayüz ve işlem hattı ayrı, böylece GIL yüzünden arayüz hattı yavaşlatamaz.

```
┌──────────────────────── UI Process (PySide6) ────────────────────────┐
│ MainWindow (Canlı / Modeller / Profiller / Ayarlar)   Tray   Hotkeys  │
│ Overlay (çeviri)   RegionFrame (F8)   Inspector (F8)   WebServer+QR   │
│ ModelManager ── HTTP ──► Ollama (/api/tags, show, ps, pull, delete)   │
└──────────▲────────────────────────────────┬──────────────────────────┘
     events │ (TranslationEvent, DebugFrame,  │ commands (Start/Stop,
            │  Status, Latency)              │ SetRegion, SetProfile…)
┌───────────┴──── Engine Process (multiprocessing) ▼────────────────────┐
│ Capture thread ─► Vision ─► OCR thread ─► Text ─► Translation Orchestr.│
│  (WGC: pencere   crop, 2x,   WinRT OCR     filtre,   ├─ GPU lane (hızlı)│
│   / monitör /    renk maskesi,             2-kare    └─ CPU lane (kalite,│
│   video cihazı)  değişim algılama          teyidi,       iptal edilebilir)│
│                                            önbellek  ── HTTP ──► Ollama │
└───────────────────────────────────────────────────────────────────────┘
```

- Engine process içinde hafif thread'ler (yakalama, OCR, çeviri hatları); numpy/OpenCV/WinRT/HTTP GIL'i bırakır.
- IPC: `multiprocessing.Queue` üzerinden tipli dataclass mesajlar.
- Debug görüntüleri yalnızca ayar modunda ve ~5 fps sınırıyla gönderilir; normal modda hiç gönderilmez.
- Overlay UI tarafında; çevirinin ekrana gelmesi birkaç ms.

## 2. İşlem hattı
| Aşama | Yöntem |
|---|---|
| Yakalama | `windows-capture` (Windows Graphics Capture): pencere bazlı (üstü kapalı olsa bile) ve monitör yakalama; Win11'de sarı çerçeve kapatılabilir. Yedek: `mss`. FPS ayarlanabilir, varsayılan 10. |
| Bölge koordinatı | Pencerenin client alanına göre normalize (0–1); pencere taşınsa/boyutlansa da doğru yerde kalır. DPI'a göre fiziksel piksele çevrilir. |
| Ön işleme | Kesme + 2x büyütme (INTER_CUBIC), ayarlı altyazı renklerine göre HSV/mesafe maskesi, isteğe bağlı kontur desteği (yazının etrafındaki koyu kenarı şart koşma), morfolojik temizlik, boyut/oran filtresiyle bileşen ayıklama; çıktı beyaz zeminde siyah yazı. Her adım açılıp kapatılabilir, eşikler ayarlanabilir. |
| Değişiklik algılama | Sadece yazı maskesi karşılaştırılır (küçültülmüş maske, piksel farkı / IoU). Arka plan hareketi tetiklemez. Maske boşalırsa altyazı kaybolmuş sayılır, overlay ayarlanabilir süre sonra temizlenir. |
| OCR | WinRT OcrEngine en-US. Açılışta dil paketi kontrolü; yoksa talimat + UAC ile `Add-WindowsCapability -Online -Name Language.OCR~~~en-US~0.0.1.0` çalıştıran "Kur" düğmesi. |
| 2-kare teyidi | Normalize metin iki ardışık okumada aynı olmalı (rapidfuzz ≥ 0.9). Değişiklik algılanınca ikinci okuma ~50 ms sonra hemen yapılır. |
| Filtreler | Harf oranı, en kısa uzunluk, sesli harf, tekrar eden çöp karakterler; son gönderilen satırın tekrarı elenir. |
| Önbellek | Bellekte LRU + diskte SQLite. Anahtar: (model, profil, normalize metin). Bağlam anahtara katılmaz (kasıtlı). |

## 3. Çeviri
- `TranslationBackend` arayüzü: `translate(req, cancel) -> Result`.
- İlk uygulama `OllamaBackend`: httpx ile `/api/chat` stream; `think: false`, `options.num_ctx` (varsayılan 2048), `options.num_gpu`, `keep_alive`. İptal = stream'i kapatmak. İleride başka backend'ler (llama.cpp, kendi fine-tune modelim) aynı arayüzle eklenir.
- Orchestrator:
  - GPU hattı aynı anda tek iş; "en yeni kazanır" — ekrandan çıkmış satırlar çevrilmez.
  - CPU kalite hattı varsayılan kapalı. GPU sonucu gösterildikten sonra altyazı hâlâ ekrandaysa CPU modeline (`num_gpu: 0`) gider, sonuç GPU çevirisinin yerine geçer; altyazı değişirse iptal. Mod kapatılınca model `keep_alive: 0` ile bellekten çıkarılır.
- Prompt: sistem talimatı (argo/küfür doğal Türkçeye, isim ve yer adları çevrilmez, yalnızca çeviri döner) + profile özel bağlam ve terim listesi + son N satır bağlam (varsayılan 3).
- Her sonuç kaynak modeli, hattı (GPU/CPU), OCR ve çeviri sürelerini (ms) taşır; canlı akışta görünür.

## 4. Model yönetimi
- Liste: `/api/tags`, `/api/show`, `/api/ps` → parametre sayısı, quantization, disk boyutu, yüklü mü.
- VRAM tahmini: ağırlık + KV cache (model_info'daki katman, kv-head, head-dim ve num_ctx'ten) + sabit pay → sığar / sınırda / sığmaz.
- Oyun için ayrılan VRAM payı (profil başına — bkz. düzeltme 3); etiketler bu paydan kalan bütçeye göre.
- Çalışırken: `/api/ps`'te `size_vram < size` ise uyarı.
- İndirme/silme: `/api/pull` stream ile ilerleme + iptal; silme onay sorar.
- Önerilen modeller `recommended_models.json`'da (isimler Ollama kütüphanesinden doğrulanacak):
  - GPU: `qwen3:8b`, `aya-expanse:8b`, `gemma4:12b-it-qat`, `translategemma:4b`
  - CPU: `qwen3:30b-a3b`
- Test/karşılaştırma: sabit örnek oyun cümlesi seti iki modelle yan yana, ms süreleriyle.

## 5. Arayüz ve Windows tarafı
- Overlay: `FramelessWindowHint | WindowStaysOnTopHint | Tool` + `WA_TranslucentBackground`. Tıklama geçirgenliği: Win32 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE` (ayar modunda kapalı). Yakalamadan hariç tutma: `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (bölge çerçevesi ve inspector'a da). Yazı `QPainterPath` ile (temiz kontur/gölge).
- Kısayollar: Win32 `RegisterHotKey` + Qt native event filter (low-level hook yok: anti-cheat dostu, yönetici izni gerekmez).
- Exclusive fullscreen: `SHQueryUserNotificationState` ile algılanır, borderless windowed önerilir. Tamamen siyah kare (korumalı içerik) ayrıca algılanıp uyarılır.
- Web çıktısı: stdlib `ThreadingHTTPServer` + Server-Sent Events, `qrcode` ile QR. Güvenlik duvarı izni arayüzde anlatılır.
- Config/profiller: `%APPDATA%\LiveSubs\` altında `config.json` ve `profiles/*.json`; Pydantic ile doğrulama, şema versiyonlu. Açılışta son profil.
- Tema: `theme/base.qss` şablonu + `vice.json` / `dark.json` renk token'ları. Fontlar (OFL, Google Fonts): logo Monoton veya Neonderthaw, başlık Audiowide, gövde Inter. İkon ve başlık görseli kendi SVG'lerimiz. Glow sadece logo ve Başlat düğmesinde.

## 6. Proje yapısı
```
LiveSubs/
├─ pyproject.toml   README.md   README.tr.md   LICENSE   CLAUDE.md
├─ assets/          (icon.svg, header.svg, fonts/ + OFL lisansları)
├─ src/livesubs/
│  ├─ app.py, __main__.py
│  ├─ core/         config.py, profiles.py, paths.py, messages.py (IPC tipleri)
│  ├─ engine/       process.py, pipeline.py
│  │  ├─ capture/   base.py, wgc.py, mss_fallback.py, video_device.py
│  │  ├─ vision/    preprocess.py, change.py
│  │  ├─ ocr/       winrt_ocr.py, language.py
│  │  ├─ text/      normalize.py, filters.py, stabilizer.py
│  │  └─ translate/ base.py, ollama.py, orchestrator.py, prompt.py, cache.py
│  ├─ ollama/       client.py, catalog.py, vram.py, recommended_models.json
│  ├─ platform/     win32.py (affinity, click-through, hotkey, pencere listesi, DPI, fullscreen)
│  ├─ ui/           main_window.py, overlay.py, region_frame.py, inspector.py, tray.py,
│  │                setup_check.py, tabs/, theme/
│  └─ web/          server.py, static/index.html
├─ tests/           samples/ (+ samples.json: bölge, renk, beklenen metin), test_*.py
├─ scripts/         build_exe.ps1, livesubs.spec, bench_pipeline.py
└─ .github/workflows/ci.yml   (windows-latest: ruff + pytest)
```
- Uygulama bağımlılıkları: PySide6, numpy, opencv-python-headless, windows-capture, mss, winrt-* (Media.Ocr, Graphics.Imaging, Storage.Streams, Globalization), httpx, pydantic, rapidfuzz, qrcode, platformdirs
- Geliştirme: pytest, pytest-qt, respx, ruff, pyinstaller
- Testler: ön işleme/filtreler `tests/samples` + `samples.json` ile veri güdümlü; OCR testleri `@pytest.mark.ocr` (paket yoksa ve CI'da atlanır); Ollama testleri respx ile sahte HTTP.

## 7. Adımlar (her biri bir veya birkaç anlamlı commit)
**Faz 1 — Çalışan çekirdek**
1. İskelet: pyproject, ruff/pytest, LICENSE, CLAUDE.md, README taslağı, CI.
2. Platform + config: Win32 yardımcıları, config/profil modelleri.
3. Yakalama: WGC pencere/monitör, bölge kesme, bench scripti (kaynak soyutlaması video cihazına uygun).
4. Vision: ön işleme + değişiklik algılama + testler.
5. OCR: WinRT sarmalayıcı, dil paketi kontrolü + testler.
6. Metin: normalize, filtreler, 2-kare stabilizer + testler.
7. Çeviri: Ollama backend, prompt, önbellek, orchestrator (yalnızca GPU) + sahte HTTP testleri.
8. Engine process + IPC.
9. Minimal UI: kaynak seçimi, bölgeyi sürükleyerek seçme, Başlat/Durdur, canlı akış, overlay. → İlk hedef: kaynak seç → bölge seç → OCR → GPU çevirisi → overlay.

**Faz 2 — Özellikler**
10. F8 ayar modu (taşınabilir çerçeve, inspector, overlay taşıma/boyutlandırma) + kısayollar.
11. Oyun profilleri.
12. Modeller sekmesi (liste, VRAM etiketi, indirme/iptal, silme, önerilenler, test/karşılaştırma).
13. CPU kalite modu.
14. Ayarlar sekmesi, açılış kontrolü sihirbazı, tepsi, fullscreen uyarısı, kayıt modu.
15. Video cihazı (capture kartı) kaynağı.
16. Vice teması, fontlar, SVG ikon/başlık görseli, overlay stilleri.
17. Web çıktısı + QR (+ Wake Lock).

**Faz 3 — Yayın**
18. PyInstaller onedir build scripti (onedir zip olarak GitHub Release). Erken bir "smoke build" Faz 1 sonunda yapılacak.
19. README (mimari şema, kurulum, GIF yeri), ekran görüntüleri.

**Gecikme hedefi (GPU'da 8B model):** yakalama + ön işleme ~10 ms, OCR ~30–80 ms, 2-kare teyidi ~50 ms, çeviri ~250–600 ms → toplam ~0,4–0,8 s. `bench_pipeline.py` ölçecek.

## 8. Bilinen riskler
- DPI ölçekleme: Qt mantıksal koordinatları ↔ yakalamanın fiziksel pikselleri; baştan uygulanıp test edilecek.
- İki modelin aynı anda yüklü kalması: `OLLAMA_MAX_LOADED_MODELS` ≥ 2; uygulama kontrol edip talimat gösterecek.
- CPU modeli RAM'i: Qwen3 30B-A3B Q4 ~18–19 GB; 32 GB'ta oyunla birlikte sınırda, ayarlarda belirtilecek.
- PyInstaller: winrt ve windows-capture native modülleri için spec'te ek ayar gerekebilir; erken smoke build ile doğrulanacak.

---

## Onaylanan düzeltme ve eklemeler (plandan önceliklidir)

1. **Git:** winget ile kur (`winget install --id Git.Git -e`). PATH için oturumun yenilenmesi gerekirse kullanıcıya söyle.
2. **Dil ve araçlar:** arayüz Türkçe (tek yerde toplanmış, çeviriye hazır); kod, yorumlar ve commit mesajları İngilizce; README İngilizce + README.tr.md. Lisans MIT. Python 3.13 venv + pip + pyproject.toml.
3. **Oyun için ayrılan VRAM** sabit 2 GB değil, profil başına: Remote Play / capture kartı kaynaklı profillerde varsayılan ~0.5 GB (oyun PC'de çalışmıyor, sadece video çözme), PC oyunu profillerinde ~2 GB+. VRAM etiketleri aktif profile göre hesaplanır.
4. **Kayıt modu:** WDA_EXCLUDEFROMCAPTURE overlay'i OBS/ekran görüntüsünden de gizliyor; README GIF'i ve LinkedIn videosu çekilecek. Ayarlarda "Kayıt modu": overlay yakalamada görünür olur; overlay altyazı bölgesiyle çakışıyorsa uyarı verilir.
5. **Gemma modeli** önerilenlerde `gemma4:12b-it-qat` olarak yazılır (Ollama'dan doğrulanır).
6. **Video cihazı (capture kartı) kaynağı:**
   - Bağlı video cihazları listelenir (Media Foundation; OpenCV CAP_MSMF, gerekirse CAP_DSHOW yedek); cihaz/çözünürlük/fps seçilebilir.
   - Önizleme penceresi gerektirmeden doğrudan okunur; ayar modunda (F8) bölge seçimi için o anki kare inspector'da gösterilir.
   - Cihaz başka uygulama (OBS vb.) tarafından kullanılıyorsa açık hata + "pencere kaynağı olarak OBS önizlemesini seç" önerisi.
   - Kaynak tamamen siyahsa HDCP uyarısı (PS5: Ayarlar → Sistem → HDMI → HDCP'yi kapat).
   - Bu kaynakta overlay'in nerede gösterileceği ayrı ayarlanır (oyun monitörü PC'ye bağlı değilse PC ekranı veya telefon/web çıktısı).
   - Faz 2'de eklenir, ama `capture/base.py` soyutlaması şimdiden buna uygun tasarlanır.
7. **Web çıktısı:** Screen Wake Lock API ile telefon ekranı kararmasın; desteklenmiyorsa sayfada "ekran zaman aşımını kapat" ipucu. Sayfa Vice temasında, büyük ve okunaklı yazıyla, yatay/dikey uyumlu.