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
    "tab.appearance": "Görünüm",
    "appearance.placement": "Çevirinin yeri",
    "appearance.placement.above": "Altyazının üstünde",
    "appearance.placement.over": "Altyazının yerinde (İngilizcenin üstünü kaplar)",
    "appearance.placement.below": "Altyazının altında",
    "appearance.placement.custom": "Serbest (F8 ayar modunda taşıyıp boyutlandırın)",
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
    # Ollama
    "ollama.checking": "Ollama kontrol ediliyor…",
    "ollama.connected": "Ollama bağlı",
    "ollama.offline": "Ollama çalışmıyor",
    "ollama.start": "Ollama'yı başlat",
    "ollama.not_installed": "Ollama kurulu değil (ollama.com)",
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
        "İngilizce OCR paketi yüklü değil. Windows Ayarlar > Saat ve dil > Dil'den "
        "İngilizce (ABD) ekleyin."
    ),
    "error.ollama_unavailable": "Ollama'ya ulaşılamıyor; yalnızca İngilizce gösteriliyor.",
    "error.model_not_found": "Model yüklü değil: {detail}",
    "error.translation_failed": "Çeviri başarısız: {detail}",
    "error.internal": "Beklenmeyen hata: {detail}",
    "error.no_source": "Önce bir kaynak seçin.",
    "error.no_region": "Önce altyazı bölgesini seçin.",
    "error.region_needs_screen": "Seçilen alan hiçbir ekranda değil; tekrar deneyin.",
}
