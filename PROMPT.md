# LiveSubs — Proje Tanımı

Sıfırdan bir Windows masaüstü uygulaması yapmak istiyorum: LiveSubs. Herhangi bir oyundaki (PC oyunları, PS5 Remote Play, emülatör vb.) İngilizce altyazıyı ekrandan okuyup gerçek zamanlı ve tamamen yerel olarak Türkçeye çeviren, çeviriyi ekranda istediğim yerde gösteren genel bir araç. GitHub'a koyup LinkedIn'de portföy projesi olarak paylaşacağım; kod kalitesi, proje yapısı ve README profesyonel olmalı.

## Temel beklentiler
- Gerçek bir uygulama: .exe'ye çift tıklayıp açılmalı, terminal gerektirmemeli.
- Python 3.13 + PySide6, PyInstaller ile .exe. (Python 3.15'te winrt/numpy derleme hatası veriyor, kullanma.)
- Donanım: i7-14700HX, RTX 4070 Laptop (8 GB VRAM), 32 GB RAM. Oyun da aynı PC'de çalışıyor olabilir; uygulamanın kendisi hafif olmalı.
- Bulut API YOK. Çeviri Ollama ile yerelde.
- Gecikme her şeyden önemli. Yakalama, OCR ve çeviri arayüzden ayrı thread/process'te çalışmalı; arayüz döngüyü asla yavaşlatmamalı.

## Kaynak ve altyazı bölgesi
- Kaynak: açık pencereler listesinden seçim (pencere bazlı yakalama) veya monitör/ekran bölgesi.
- Global kısayol tuşu (ör. F8) ile "ayar modu":
  - Seçili altyazı bölgesi ekranda çerçeve olarak görünsün; fareyle taşıyıp yeniden boyutlandırabileyim.
  - OCR'ın o an okuduğu metin ve ön işlenmiş görüntü canlı görünsün, doğru okuyup okumadığını görebileyim.
  - Çeviri overlay'i de aynı modda taşınıp boyutlandırılabilsin.
- Oyun başına profil: altyazı bölgesi, renk/eşik ayarları, overlay konumu ve stili, çeviri bağlamı her oyun için ayrı kaydedilsin; profil seçilebilsin. Açılışta son kullanılan profil gelsin.

## OCR ve oyun arka planı
- Windows yerleşik OCR (winrt) kullan; hızlı ve iyi çalışıyor. Türkçe Windows'ta İngilizce OCR paketi (Language.OCR~~~en-US~0.0.1.0) yüklü değil; uygulama bunu algılayıp nasıl kurulacağını göstersin.
- Altyazının arkasında hareketli, parlak oyun görüntüsü olacak. Buna karşı:
  - Ön işleme: bölgeyi 2x büyüt, ayarlanabilir altyazı rengine (beyaz/sarı vb.) göre maskele, arka planı temizle. Açık/kapalı ve eşikler ayarlanabilsin.
  - Değişiklik algılama sadece yazı piksellerine bakmalı; arka plan hareketi yeni OCR/çeviri tetiklememeli.
  - Aynı metin art arda en az 2 karede okunmadan çeviriye gönderme.
  - Anlamsız / harf oranı düşük satırları at.
  - Çeviri önbelleği (tekrar eden satırlar anında gelsin).

## Çeviri overlay'i
- Çerçevesiz, her zaman üstte, yarı şeffaf pencere; oyunun üstünde görünecek.
- Overlay ekran yakalamadan hariç tutulmalı (SetWindowDisplayAffinity / WDA_EXCLUDEFROMCAPTURE), yoksa OCR kendi çevirisini okur.
- Normal modda tıklamalar içinden geçsin; ayar modunda taşınıp boyutlandırılabilsin.
- Ayarlanabilir: konum, boyut, font, yazı boyutu, renk, kontur/gölge, arka plan opaklığı, gösterilecek satır sayısı.
- İsteğe bağlı: İngilizce orijinali küçük olarak altında göster.
- Exclusive fullscreen oyunlarda overlay görünmez; uygulama bunu kullanıcıya açıklasın (borderless windowed önerisi).
- Ek çıktı olarak isteğe bağlı yerel web sayfası (aynı Wi-Fi'daki telefonda gösterim) + QR kod.
- Kısayollar: ayar modu, overlay'i gizle/göster, çeviriyi duraklat/devam.

## Çeviri
- Ollama üzerinden; model Modeller sekmesinden seçilsin. TranslateGemma 4B hızlıydı (~250 ms/satır) ama oyun argosunda kalitesi çok kötüydü; Qwen3 8B, Aya Expanse 8B gibi modelleri deneyeceğim, model değiştirmek kolay olmalı.
- Varsayılan: sadece GPU'da tek model.
- CPU kalite modu (ayarlardan açılıp kapatılabilir, varsayılan kapalı):
  - Önce GPU'daki hızlı model çeviriyi gösterir.
  - Altyazı hâlâ ekrandaysa CPU'da çalışan kaliteli model (varsayılan Qwen3 30B-A3B) çevirisi onun yerine geçer.
  - Altyazı değişirse CPU'daki eski iş iptal edilsin.
  - CPU modeli ve GPU/CPU ataması (Ollama num_gpu) ayarlardan seçilsin.
  - Kapalıyken CPU modeli hiç yüklenmesin, RAM kullanmasın.
- Canlı akışta her çevirinin hangi modelden geldiği ve süresi görünsün; iki modu karşılaştırıp test edebileyim.
- Prompt'ta oyun bağlamı: argo, küfür ve sokak dili doğal Türkçeye; karakter ve yer adları çevrilmez. Profil başına ek bağlam / terim listesi verilebilsin.
- Önceki birkaç satır bağlam olarak modele verilsin (konuşma akışı için).
- Ollama isteklerinde num_ctx küçük tutulsun (varsayılan 2048, ayarlanabilir) ki 12B modeller (ör. gemma4:12b-it-qat) 8 GB VRAM'e sığsın. Qwen3 gibi düşünen modellerde thinking kapatılsın (think: false). Model VRAM'e tam sığmıyorsa (/api/ps ile kontrol) uyarı gösterilsin.
- Mimari ileride başka backend'ler ve fine-tune edilmiş kendi modelim eklenebilecek şekilde olsun.

## Model yönetimi (uygulama içinden)
- "Modeller" sekmesi: Ollama'da yüklü tüm modeller listelensin (Ollama API: /api/tags, /api/show, /api/ps).
  - Her model için: isim, parametre sayısı, quantization, disk boyutu, tahmini VRAM/RAM ihtiyacı, şu an bellekte yüklü mü.
  - 8 GB VRAM'e sığıp sığmadığı işaretlensin (sığar / sınırda / sığmaz).
- Modeli seçip "GPU modeli" veya "CPU kalite modeli" olarak atayabileyim; ana pencerede o an kullanılan modellerin adı görünsün.
- Yeni model indirme: model adını yazıp (ör. qwen3:8b) indir; ilerleme çubuğu, indirilen/kalan boyut, iptal (/api/pull stream).
- Önerilen modeller listesi: çeviri için uygun birkaç model, kısa açıklama ve boyutlarıyla, tek tıkla indir.
- Model silme (onay sorarak).
- Seçilen model için "test et" düğmesi: örnek oyun cümleleri çevrilsin, süre (ms) ve çeviri yan yana gösterilsin; iki modeli aynı cümlelerle karşılaştırabileyim.
- Ollama kurulu/çalışmıyorsa bu sekme ne yapılacağını göstersin.

## Ana pencere
- Kaynak ve profil seçimi, Ollama durumu (bağlı mı, model yüklü mü), kullanılan modellerin adı, büyük Başlat/Durdur.
- Canlı akış: İngilizce satır, Türkçe çeviri, kaynak model, gecikme (OCR ve çeviri ayrı ms).
- Ayarlar sekmesi (fps, eşikler, dil, port, kısayollar); config dosyasına kaydedilsin.
- Açılışta eksik kontrolü (Ollama, modeller, OCR paketi) ve adım adım çözüm talimatı.
- Sistem tepsisine küçülebilsin.

## Görsel tasarım (Vice City / 80'ler Miami havası)
- Arayüz GTA 6 / Vice City havasında olsun: gün batımı ve neon Miami, 80'ler synthwave.
- Renk paleti: koyu mor/gece mavisi zemin; neon pembe/magenta, turkuaz/cyan, gün batımı turuncusu vurgular; pembe→turuncu→mor gradyanlar.
- Başlıklar ve logo için retro, neon tabela hissi veren ücretsiz/açık lisanslı bir font (lisansını README'de belirt); gövde metni okunaklı sade bir font.
- Hafif neon parlama (glow) efektleri, gradyanlı Başlat düğmesi, palmiye/gün batımı motifli sade bir uygulama ikonu ve başlık görseli (kendi çizimimiz, SVG).
- Rockstar/GTA logosu, resmi fontu, oyun görselleri veya marka adı KULLANILMAYACAK; sadece esinlenme. Uygulama adı LiveSubs kalır.
- Tema tek bir QSS dosyasında ve renk token'larıyla tanımlansın; ayarlardan tema değiştirilebilsin (Vice tema varsayılan + sade koyu tema).
- Overlay için de "Vice" hazır stili olsun (pembe/cyan kontur), ama okunabilirlik her zaman öncelik.
- Animasyonlar hafif olsun; performansı ve gecikmeyi etkilememeli.

## Çalışma şekli
- Önce mimariyi ve planı çıkar; onayımdan sonra adım adım yaz.
- İlk hedef çalışan bir çekirdek: kaynak seç → bölge seç → OCR → GPU çevirisi → overlay. Sonra diğer özellikler.
- Her adım ayrı, anlamlı commit. Testler yaz (ön işleme ve filtreler için örnek oyun ekran görüntüleriyle; tests/samples klasörüne koyacağım).
- Sonunda: README (özellikler, kurulum, ekran görüntüsü/GIF yeri, mimari şema), .exe build scripti, lisans.
- Proje bağlamını, kararları ve ilerlemeyi CLAUDE.md'de tut ve her adımda güncelle.