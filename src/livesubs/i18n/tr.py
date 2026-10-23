"""Turkish UI text."""

STRINGS: dict[str, str] = {
    # Main window
    "app.subtitle": "Oyun altyazılarını yerelde Türkçeye çevirir",
    "source.label": "Kaynak",
    "source.refresh": "Yenile",
    "source.window": "Pencere: {title}",
    "source.monitor": "Monitör: {name} ({width}x{height})",
    "source.none": "Kaynak seçin",
    "region.select": "Altyazı bölgesini seç",
    "region.selected": "Bölge: {source} — konum {x:.0%}, {y:.0%}, boyut {w:.0%} x {h:.0%}",
    "region.none": "Bölge seçilmedi",
    "region.hint": (
        "Altyazının çıktığı alanı fareyle çizin (hangi ekranda olursa olsun).\n"
        "Altındaki pencere kaynak olarak seçilir. Enter / çift tık: onayla, Esc: vazgeç"
    ),
    "model.label": "Model",
    "model.none": "Model yok",
    "start": "Başlat",
    "stop": "Durdur",
    "feed.time": "Saat",
    "feed.english": "İngilizce",
    "feed.turkish": "Türkçe",
    "feed.model": "Model",
    "feed.ocr_ms": "OCR (ms)",
    "feed.translate_ms": "Çeviri (ms)",
    "feed.pending": "…",
    "feed.cached": "önbellek",
    # Overlay
    "overlay.sample": "Örnek çeviri: Kıçını arabaya sok kanka, aynasızlar geliyor!",
    "overlay.sample_original": "Get your ass in the car, bro. The cops are coming!",
    # Appearance tab
    "tab.live": "Canlı",
    "header.tagline": "Oyun altyazıları için canlı Türkçe çeviri",
    "tab.appearance": "Görünüm",
    "appearance.style": "Hazır stil",
    "appearance.style.classic": "Klasik (beyaz yazı, siyah kontur)",
    "appearance.style.vice": "Vice (pembe kontur, camgöbeği hale)",
    "appearance.style.box": "Kutu (kontursuz, koyu kutu)",
    "appearance.style_hint": "Stil renkleri ve konturu ayarlar; sonra tek tek değiştirebilirsiniz.",
    "appearance.placement": "Çevirinin yeri",
    "appearance.placement.above": "Altyazının üstünde",
    "appearance.placement.over": "Altyazının yerinde (İngilizcenin üstünü kaplar)",
    "appearance.placement.below": "Altyazının altında",
    "appearance.placement.custom": "Serbest (F8 ayar modunda taşıyıp boyutlandırın)",
    "appearance.placement_on_monitor": (
        "Profilde ayrı bir ekran seçili: çeviri o ekranda F8 ile taşındığı yerde durur."
    ),
    "appearance.box": "Arka plan",
    "appearance.background": "Siyah kutu",
    "appearance.font": "Yazı tipi",
    "appearance.colors": "Renkler",
    "appearance.text_color": "Yazı",
    "appearance.outline": "Kontur",
    "appearance.shadow": "Gölge",
    "appearance.pick_color": "Renk seçin",
    "appearance.max_lines": "En fazla satır",
    "appearance.show_original": "İngilizce aslını altında küçük göster",
    "appearance.clear_delay": "Altyazı bitince ekranda kalma",
    "appearance.preview": "Önizle",
    "appearance.hint": (
        "Değişiklikler hemen uygulanır ve kaydedilir. Önizle, örnek bir çeviriyi seçtiğiniz "
        "bölgeye göre gösterir. Serbest konumda overlay'i F8 ile ayar moduna geçip fareyle "
        "taşıyabilir ve kenarlarından boyutlandırabilirsiniz."
    ),
    # Calibration and hotkeys
    "calibration.button": "Ayar modu (F8)",
    "calibration.on": (
        "Ayar modu: overlay'i ve altyazı bölgesini sürükleyin, kenarlarından boyutlandırın. "
        "Çıkmak için F8."
    ),
    "calibration.off": "Ayar modu kapandı; değişiklikler kaydedildi.",
    "region_frame.label": "Altyazı bölgesi",
    "overlay.hidden": "Çeviri gizlendi (göstermek için F9)",
    "overlay.shown": "Çeviri gösteriliyor",
    "hotkey.failed": "Kısayol kaydedilemedi, başka bir uygulama kullanıyor olabilir: {keys}",
    # Inspector (calibration)
    "inspector.title": "LiveSubs — Inspector",
    "inspector.waiting": "Görüntü bekleniyor… (çeviri çalışırken görünür)",
    "inspector.region": "Altyazı bölgesi (yakalanan görüntü)",
    "inspector.ocr_input": "OCR'ın okuduğu (ön işlenmiş görüntü)",
    "inspector.stats": "Yazı pikseli: {fraction:.1%} — Değişim: {change}",
    "inspector.no_read": "Son okuma: henüz yok",
    "inspector.read_ok": "Son okuma: “{text}” — geçti",
    "inspector.read_rejected": "Son okuma: “{text}” — elendi ({reason})",
    "inspector.settings": "Ayarlar (hemen uygulanır)",
    "inspector.colors": "Altyazı rengi",
    "inspector.white": "Beyaz",
    "inspector.yellow": "Sarı",
    "inspector.custom": "Özel",
    "inspector.tolerance": "Renk toleransı",
    "inspector.contrast": "Yerel kontrast",
    "inspector.local_contrast": "Açık",
    "inspector.hint": (
        "Alt görüntüde yazı siyah ve net, geri kalan beyaz olmalı. Harfler eksikse toleransı "
        "artırın; arka plandan lekeler geliyorsa toleransı azaltın ya da kontrastı yükseltin."
    ),
    "change.none": "yazı yok",
    "change.same": "aynı satır",
    "change.appeared": "yeni satır belirdi",
    "change.changed": "satır değişti",
    "change.cleared": "satır kayboldu",
    "reject.empty": "boş",
    "reject.too_short": "çok kısa",
    "reject.few_letters": "harf oranı düşük",
    "reject.no_vowels": "sesli harf yok",
    "reject.repeated_junk": "tekrar eden çöp karakter",
    # Ollama
    "ollama.checking": "Ollama kontrol ediliyor…",
    "ollama.connected": "Ollama bağlı",
    "ollama.offline": "Ollama çalışmıyor",
    "ollama.start": "Ollama'yı başlat",
    "ollama.not_installed": "Ollama kurulu değil (ollama.com)",
    # Setup (first run, fresh Windows install)
    "setup.button": "Kurulum…",
    "setup.title": "LiveSubs — Kurulum",
    "setup.intro": (
        "LiveSubs'ın çalışması için gerekenler aşağıda. Eksik olanları buradan kurabilirsiniz; "
        "hepsi yeşil olunca pencereyi kapatıp Başlat'a basın."
    ),
    "setup.ocr": "İngilizce OCR",
    "setup.ocr.ok": "✔ Yüklü ({language})",
    "setup.ocr.missing": "✘ Yüklü değil — altyazılar okunamaz",
    "setup.ocr.install": "Yükle (yönetici izni)",
    "setup.ocr.installing": "Kuruluyor… Açılan PowerShell penceresi kapanınca hazır olur.",
    "setup.ocr.declined": "Yönetici izni verilmedi.",
    "setup.ocr.restart": "Hâlâ görünmüyor: kurulum bittiyse LiveSubs'ı yeniden başlatın.",
    "setup.ollama": "Ollama",
    "setup.ollama.ok": "✔ Kurulu ve çalışıyor",
    "setup.ollama.stopped": "✘ Kurulu ama çalışmıyor",
    "setup.ollama.missing": "✘ Kurulu değil — çeviri yapılamaz",
    "setup.ollama.install": "Kur",
    "setup.ollama.start": "Başlat",
    "setup.ollama.installing": "Ollama indirilip kuruluyor (~1 GB, birkaç dakika sürebilir)…",
    "setup.ollama.starting": "Ollama başlatılıyor (ilk açılışta 1 dakikayı bulabilir)…",
    "setup.ollama.failed": "Kurulum başarısız: {detail}. İndirme sayfası açılıyor.",
    "setup.ollama.no_winget": "winget bulunamadı; Ollama indirme sayfası açıldı.",
    "setup.model": "Çeviri modeli",
    "setup.model.ok": "✔ {model} yüklü",
    "setup.model.missing": "✘ Seçili model ({model}) yüklü değil — aşağıdan indirin ya da seçin",
    "setup.model.needs_ollama": "Önce Ollama gerekli",
    "setup.model.download": "İndir",
    "setup.model.use": "Bunu kullan",
    "setup.model.in_use": "Kullanılıyor",
    "setup.model.cancel": "İptal",
    "setup.model.progress": "{status}: {done:.1f} / {total:.1f} GB",
    "setup.model.progress_unknown": "{status}…",
    "setup.model.cancelled": "İndirme durduruldu; tekrar İndir'e basınca kaldığı yerden sürer.",
    "setup.model.failed": "İndirme başarısız: {detail}",
    "setup.model.no_space": (
        "Diskte yer yok: {need:.1f} GB gerekli, {free:.1f} GB boş ({folder})."
    ),
    "setup.model.recommended": " (bu bilgisayar için önerilen)",
    "setup.model.preparing": "Modelin görüntü bölümü olmayan, daha hızlı kopyası hazırlanıyor…",
    "setup.model.text_only_failed": (
        "Hızlı kopya hazırlanamadı ({detail}); model olduğu gibi kullanılıyor."
    ),
    "catalog.gemma4_26b": "En iyi çeviri, ~1,15 sn/satır, 32 GB RAM ister — {size:.1f} GB",
    "catalog.gemma4_12b": "Çok iyi çeviri, ~1,2 sn/satır, 16 GB RAM yeter — {size:.1f} GB",
    "setup.shortcut": "Kısayol",
    "setup.shortcut.ok": "✔ Masaüstünde ve Başlat menüsünde",
    "setup.shortcut.missing": "Masaüstü ve Başlat menüsü kısayolu yok",
    "setup.shortcut.create": "Oluştur",
    "setup.shortcut.recreate": "Bu kopyaya yeniden oluştur",
    "setup.shortcut.failed": "Kısayol oluşturulamadı.",
    "setup.backup": "Ayarlar",
    "setup.backup.hint": "Format öncesi dışa aktarın, kurulumdan sonra içe aktarın.",
    "setup.export": "Dışa aktar…",
    "setup.import": "İçe aktar…",
    "setup.export.title": "Ayarları dışa aktar",
    "setup.import.title": "Ayarları içe aktar",
    "setup.backup.filter": "LiveSubs ayarları (*.zip)",
    "setup.backup.default_name": "LiveSubs-ayarlar.zip",
    "setup.export.done": "{count} dosya kaydedildi: {path}",
    "setup.import.done": "{count} dosya içe aktarıldı.",
    "setup.backup.failed": "İşlem başarısız: {detail}",
    "setup.restart.title": "Yeniden başlatma",
    "setup.restart.text": (
        "İçe aktarılan ayarlar LiveSubs yeniden başlayınca geçerli olur. "
        "Şimdi yeniden başlatılsın mı?"
    ),
    "setup.close": "Kapat",
    # Profiles
    "profile.label": "Profil",
    "tab.profiles": "Profiller",
    "profiles.manage": "Profil",
    "profiles.active": "Etkin profil: <b>{name}</b>",
    "profiles.new": "Yeni…",
    "profiles.copy": "Kopyala…",
    "profiles.rename": "Yeniden adlandır…",
    "profiles.delete": "Sil",
    "profiles.name": "Profil adı:",
    "profiles.copy_name": "{name} (kopya)",
    "profiles.delete_confirm": "“{name}” profili silinsin mi? Bu geri alınamaz.",
    "profiles.last_one": "Son profil silinemez.",
    "profiles.hint": (
        "Her oyun için ayrı profil açın: kaynak, altyazı bölgesi, renkler, görünüm ve çeviri "
        "ayarları profilde saklanır. Profiller arasında Canlı sekmesindeki listeden geçilir."
    ),
    "profiles.translation": "Çeviri",
    "profiles.context": "Oyun hakkında",
    "profiles.context_placeholder": (
        "Çevirmene oyunu anlatın (İngilizce ya da Türkçe), ör. \"1980'ler Miami'sinde geçen, "
        'argolu bir suç hikâyesi. Tommy bir gangster."'
    ),
    "profiles.glossary": "Sözlük",
    "profiles.glossary_add": "Satır ekle",
    "profiles.glossary_remove": "Seçili satırları sil",
    "profiles.translation_hint": (
        "Sözlükteki kelimeler her zaman böyle çevrilir; çevrilmemesi gereken isimleri aynen "
        "yazın (ör. Vice City → Vice City). Değişiklikler bir sonraki Başlat'ta geçerli olur."
    ),
    "profiles.game": "Oyun ve ekran",
    "profiles.location": "Oyun nerede çalışıyor",
    "profiles.location.auto": "Otomatik (kaynağa göre: {guess})",
    "profiles.guess.this_pc": "bu bilgisayarda",
    "profiles.guess.elsewhere": "başka cihazda",
    "profiles.location.this_pc": "Bu bilgisayarda",
    "profiles.location.elsewhere": "Başka cihazda (PS5, Remote Play, capture card)",
    "profiles.reserve": "Oyuna ayrılan ekran kartı belleği",
    "profiles.reserve_auto": "Otomatik",
    "profiles.display": "Çevirinin gösterileceği ekran",
    "profiles.display.auto": "Altyazının olduğu ekran (otomatik)",
    "profiles.display.monitor": "{name} ({width}x{height}) — F8 ile konumlandırın",
    "profiles.display.web_only": "Yalnızca telefonda (bilgisayarda çeviri gösterilmez)",
    "profiles.game_hint": (
        "Oyun bu bilgisayarda çalışıyorsa ekran kartı belleğinin bir kısmı ona kalır; model "
        "seçerken bu dikkate alınır. Oyunu TV'de oynayıp çeviriyi bilgisayar ekranında okumak "
        "için o ekranı seçin ve F8 ayar modunda çeviriyi istediğiniz yere taşıyın."
    ),
    # Models tab
    "tab.models": "Modeller",
    "models.status": "Ollama ve ekran kartı",
    "models.ollama_ok": "✔ Ollama çalışıyor — {count} model yüklü",
    "models.ollama_offline": (
        "✘ Ollama kurulu ama çalışmıyor. Başlatın; ilk açılışta 1 dakikayı bulabilir."
    ),
    "models.ollama_missing": (
        "✘ Ollama kurulu değil. Çeviri için gerekli: Kurulum… penceresinden tek tıkla kurun."
    ),
    "models.budget": (
        "Ekran kartı: {gpu}, {total:.1f} GB. Windows ve diğer uygulamalara ~{desktop:.1f} GB, "
        "oyuna {reserve:.1f} GB (profil “{profile}”) ayrılınca modele kalan: <b>{budget:.1f} GB</b>"
    ),
    "models.gpu_configured": "ayarlardan",
    "models.no_gpu": (
        "Ekran kartı bulunamadı: modeller işlemcide çalışır (yavaş). Sığma tahmini yapılamıyor."
    ),
    "models.installed": "Yüklü modeller",
    "models.column.name": "Model",
    "models.column.size": "Dosya",
    "models.column.parameters": "Parametre",
    "models.column.memory": "Gereken bellek",
    "models.column.fit": "Ekran kartına",
    "models.column.state": "Durum",
    "models.fit.fits": "✔ Sığar",
    "models.fit.borderline": "≈ Sınırda",
    "models.fit.too_big": "✘ Sığmaz",
    "models.fit.moe_split": "✔ Kısmen (MoE)",
    "models.fit.unknown": "?",
    "models.fit_tip.fits": "Model tamamen ekran kartında çalışır: en hızlısı.",
    "models.fit_tip.borderline": (
        "Ancak sığar; ekran kartını kullanan başka bir uygulama açılırsa bir kısmı işlemciye "
        "taşar ve çeviri yavaşlar."
    ),
    "models.fit_tip.too_big": (
        "Ekran kartına sığmaz: taşan katmanlar işlemcide çalışır, çeviri belirgin yavaşlar."
    ),
    "models.fit_tip.moe_split": (
        "MoE modeli: uzman katmanları RAM'de, dikkat katmanları ekran kartında durur. "
        "Bu modellerde bu normaldir ve çeviri hızlı kalır; yeterli RAM gerekir."
    ),
    "models.fit_tip.unknown": "Model bilgisi okunamadı ya da ekran kartı bulunamadı.",
    "models.fit_hint": (
        "Gereken bellek bir tahmindir. Çeviri için kullanmak istediğiniz modele çift tıklayın."
    ),
    "models.in_use": "Çeviride kullanılıyor",
    "models.loaded": "Bellekte, %{percent} ekran kartında",
    "models.use": "Çeviri için kullan",
    "models.now_in_use": "{model} artık çeviri modeli (bir sonraki Başlat'ta geçerli olur).",
    "models.delete": "Sil",
    "models.delete_confirm": (
        "{model} silinsin mi? Tekrar kullanmak için yeniden indirmeniz gerekir."
    ),
    "models.delete_in_use": "Bu model şu an çeviri modeli; silinirse başka bir model seçmelisiniz.",
    "models.delete_busy": "Çeviride kullanılan model çalışırken silinemez; önce çeviriyi durdurun.",
    "models.deleted": "{model} silindi.",
    "models.delete_failed": "Silinemedi: {detail}",
    "models.download": "Model indir",
    "models.pull_placeholder": "Model adı, ör. gemma4:12b-it-qat",
    "models.pull_hint": (
        "ollama.com/library'deki herhangi bir modelin adını yazabilirsiniz. İndirme yarıda "
        "kalırsa tekrar İndir'e basınca kaldığı yerden sürer."
    ),
    "models.pull_progress": "{status}: {done:.1f} / {total:.1f} GB (kalan {left:.1f} GB)",
    "models.pulled": "{model} indirildi.",
    "models.already_installed": "{model} zaten yüklü.",
    "models.compare": "Modelleri dene ve karşılaştır",
    "models.compare_hint": (
        "Aynı cümleler iki modelle sırayla çevrilir (önce biri, sonra diğeri); kalite ve süre "
        "yan yana görünür. Etkin profilin oyun bilgisi ve sözlüğü kullanılır. Kendi "
        "cümlelerinizi her satıra bir tane olacak şekilde yazabilirsiniz."
    ),
    "models.compare_a": "Model 1",
    "models.compare_b": "Model 2",
    "models.compare_none": "— (yalnızca Model 1)",
    "models.compare_start": "Karşılaştır",
    "models.compare_stop": "Durdur",
    "models.compare_busy": "Çeviri çalışırken karşılaştırma yapılamaz (ekran kartı kullanımda).",
    "models.compare_loading": "{model} yükleniyor… (büyük modellerde 30 sn'yi bulabilir)",
    "models.compare_running": "{model} çeviriyor…",
    "models.compare_stopping": "Durduruluyor…",
    "models.compare_failed": "{model} çalışmadı: {detail}",
    "models.compare_line_failed": "Hata: {detail}",
    "models.compare_warm": "yükleme {seconds:.1f} sn",
    "models.compare_median": "ortanca {ms:.0f} ms",
    "models.compare_gpu": "%{percent} ekran kartında",
    "models.compare_done": (
        "Bitti. Denenen modeller bellekten çıkarıldı (çeviride kullanılan hariç)."
    ),
    "models.compare_cancelled": "Durduruldu.",
    "warning.model_spill": (
        "{model} ekran kartına sığmadı: %{percent}'i ekran kartında, gerisi işlemcide; çeviri "
        "yavaşlar. Modeller sekmesinden sığan bir model seçebilirsiniz."
    ),
    # Settings tab
    "tab.settings": "Ayarlar",
    "settings.translation": "Çeviri ve okuma",
    "settings.ollama_url": "Ollama adresi",
    "settings.context_lines": "Bağlam için önceki satır",
    "settings.keep_alive": "Modeli bellekte tut",
    "settings.keep_alive.5m": "5 dakika",
    "settings.keep_alive.30m": "30 dakika",
    "settings.keep_alive.2h": "2 saat",
    "settings.keep_alive.-1": "Hep (uygulama kapanınca da)",
    "settings.fps": "Okuma hızı",
    "settings.fps_suffix": " kare/sn",
    "settings.translation_hint": (
        "Bir sonraki Başlat'ta geçerli olur. Önceki satırlar çeviriye bağlam olarak verilir "
        "(daha tutarlı, biraz daha yavaş). Okuma hızı yükseldikçe altyazı daha erken yakalanır "
        "ama işlemci daha çok çalışır; 10 çoğu oyun için yeterli."
    ),
    "settings.hotkeys": "Kısayol tuşları",
    "settings.hotkey.calibration": "Ayar modu",
    "settings.hotkey.toggle_overlay": "Çeviriyi gizle / göster",
    "settings.hotkey.pause": "Duraklat / devam",
    "settings.hotkeys_reset": "Varsayılana dön",
    "settings.hotkeys_hint": "Kutuya tıklayıp yeni tuşa basın; hemen geçerli olur.",
    "settings.hotkeys_saved": "Kısayollar kaydedildi.",
    "settings.hotkey_invalid": "Bu tuş kullanılamaz: {hotkey}",
    "settings.hotkey_taken": "{hotkey} başka bir işe atanmış.",
    "settings.window": "Görünüm, pencere ve kayıt",
    "settings.theme": "Tema",
    "settings.theme.vice": "Vice (neon gün batımı)",
    "settings.theme.dark": "Sade koyu",
    "settings.minimize_to_tray": "Simge durumuna küçültünce sistem tepsisine gönder",
    "settings.recording_mode": "Kayıt modu (OBS ve ekran görüntüleri çeviriyi de görsün)",
    "settings.recording_hint": (
        "Normalde çeviri ekran yakalamadan gizlenir; böylece OCR kendi çevirimizi okumaz. "
        "Kayıt modunda çeviri altyazı bölgesiyle çakışırsa OCR onu da okuyabilir; "
        "bu durumda uyarı gösterilir."
    ),
    "settings.web": "Telefonda göster",
    "settings.web_enabled": "Çeviriyi telefona da gönder (web sayfası)",
    "settings.web_port": "Port",
    "settings.web_only": "Çeviriyi yalnızca telefonda göster (bilgisayar ekranında kapalı)",
    "settings.web_only_tip": "Bu profil için geçerlidir (Profiller sekmesindeki ekran seçimi).",
    "settings.web_off": "Kapalı",
    "settings.web_address": "Telefonda açın: <b>{url}</b> — bağlı cihaz: {clients}",
    "settings.web_failed": "Web çıktısı başlatılamadı (port {port} kullanımda olabilir): {detail}",
    "settings.web_hint": (
        "Telefon bilgisayarla aynı Wi-Fi ağında olmalı. Kamerayla QR kodu okutun ya da adresi "
        "tarayıcıya yazın. Windows ilk seferde güvenlik duvarı izni sorarsa “Özel ağlar”a izin "
        "verin."
    ),
    "settings.upkeep": "Bakım",
    "settings.clear_cache": "Çeviri önbelleğini temizle",
    "settings.open_logs": "Günlük klasörünü aç",
    "settings.cache_cleared": "Önbellek temizlendi.",
    "settings.cache_busy": "Önce çeviriyi durdurun.",
    # Phone page (web output)
    "web.waiting": "Çeviri bekleniyor…",
    "web.connecting": "Bağlantı kuruluyor…",
    "web.hint_timeout": (
        "Ekranın kararmaması için telefonun ekran zaman aşımını uzatın. Tam ekran için dokunun."
    ),
    "web.hint_tap": "Ekranın açık kalması için bir kez dokunun.",
    # Tray and warnings
    "tray.show": "LiveSubs'ı göster",
    "tray.toggle_overlay": "Çeviriyi gizle / göster",
    "tray.quit": "Çıkış",
    "tray.running": "çalışıyor",
    "tray.idle": "hazır",
    "tray.minimized": "LiveSubs burada çalışmaya devam ediyor. Açmak için simgeye tıklayın.",
    "warning.fullscreen": (
        "Oyun özel tam ekran modunda: çeviri onun üstünde görünmez. Oyunun görüntü ayarlarından "
        '"Kenarlıksız pencere" (borderless) modunu seçin.'
    ),
    "warning.recording_overlap": (
        "Kayıt modu açık ve çeviri altyazı bölgesinin üstünde: OCR çeviriyi de okuyabilir. "
        "Çeviriyi altyazının üstüne/altına alın ya da kayıt modunu kapatın."
    ),
    # Engine states
    "state.idle": "Hazır",
    "state.starting": "Kaynak açılıyor…",
    "state.warming_up": "Model hazırlanıyor: {model} (ilk seferde 10-40 sn sürebilir)",
    "state.running": "Çalışıyor",
    "state.waiting": "Çalışıyor — altyazı bekleniyor (seçilen bölgede henüz yazı okunmadı)",
    "state.paused": "Duraklatıldı (devam için F10)",
    "state.stopped": "Durdu",
    # Errors (by EngineError kind)
    "error.source_unavailable": "Kaynak açılamadı. Pencere kapalı ya da simge durumunda olabilir.",
    "error.source_lost": "Kaynak kapandı.",
    "error.ocr_unavailable": (
        "İngilizce OCR paketi yüklü değil: Kurulum… penceresinden tek tıkla yükleyebilirsiniz."
    ),
    "error.ollama_unavailable": "Ollama'ya ulaşılamıyor; yalnızca İngilizce gösteriliyor.",
    "error.model_not_found": "Model yüklü değil: {detail}",
    "error.translation_failed": "Çeviri başarısız: {detail}",
    "error.internal": "Beklenmeyen hata: {detail}",
    "error.engine_crashed": (
        "Çeviri motoru beklenmedik şekilde kapandı ve yeniden başlatıldı. Başlat'a tekrar basın."
    ),
    "error.no_source": "Önce bir kaynak seçin.",
    "error.no_region": "Önce altyazı bölgesini seçin.",
    "error.region_needs_screen": "Seçilen alan hiçbir ekranda değil; tekrar deneyin.",
}
