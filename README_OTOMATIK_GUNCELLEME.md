# Equity Evidence Lens V3 — otomatik kanıt geçmişi ve GitHub Pages

**Durum:** 76/76 yerel test başarılı (55 devralınan V2 testi + 21 V3 testi). **GitHub üzerinde canlı workflow henüz çalıştırılmadı.** Bu arşiv mevcut site dosyalarını (`index.html`, `data/*`) **içermez/değiştirmez**; yalnızca yeni kod, test ve workflow ekler. Mevcut Git geçmişini koruyun.

## Amaç ve kapsam

- Birbirinden farklı Base Mainnet bloklarında aynı üç izinli sözleşme için `eth_getCode`, `totalSupply()`, `decimals()` salt-okunur sorguları.
- Her okumada **tek sabit blok** ve blok hash'i kullanılır. Zincir kimliği `8453` doğrulanır; blok hash'i okuma sonunda tekrar denetlenir. `latest - 32` kullanılır; **bu tek başına kesin finality garantisi değildir**.
- V2'deki iki gerçek (kamuya açık) gözlem başlangıç tarihçesi olarak korunur. Yeni gözlem `data/history.json` içine eklenir, önceki gözlemler sessizce değiştirilmez.
- Sayfada üç varlığın **örneklenmiş totalSupply** grafiği. Ara bloklardaki hareket, fiyat, gerçek hisse veya yatırım değeri çıkarımı yapılmaz.
- Yalnızca tam ve doğrulanmış gözlemde veri dosyaları yenilenir. Kullanılan RPC kaynakları güvensiz veya erişilemezse iş **hata ile durur; yayımlama adımı çalışmaz**.
- Güncellemeler GitHub Actions'ta varsayılan olarak her gün **03:17 UTC / 06:17 İstanbul** planlıdır. GitHub zamanlanmış görevleri geciktirebilir/atlayabilir, boş repo etkinliğinde durdurabilir. Bu bir SLA değildir.

## 1. Mac üzerinde güvenli kurulum (repo köküne)

Mevcut yayın reposu: `~/Desktop/equity-evidence-lens-site`. **ZIP'i `Airdrop_Lab_Equity_Lens_V2` içine çıkartmayın.**

```bash
cd ~/Desktop/equity-evidence-lens-site
git status --short
# Varsa yerel değişiklikleri gözden geçir; eklemeden önce temiz bir repo tercih edilir.
cd ~/Downloads
unzip -n Equity_Lens_V3_AutoHistory_Patch_2026-11-15.zip -d ~/Desktop/equity-evidence-lens-site
cd ~/Desktop/equity-evidence-lens-site
python3 -m unittest discover -s tests -q
python3 scripts/refresh_site.py --check-only
```

Beklenen: `Ran 76 tests`, `OK`. Ardından `LOCAL_VALIDATION_NO_NETWORK`, `history_samples: 2` ve `written: false`. Eğer doğrulama `SOURCE_MANIFEST_HASH_MISMATCH` ile durursa **otomasyonu etkinleştirmeyin**: mevcut verilerde fark olabilir.

## 2. GitHub'a yalnızca yeni kodu gönder

```bash
cd ~/Desktop/equity-evidence-lens-site
git add .gitignore .github engine scripts tests README_OTOMATIK_GUNCELLEME.md
git diff --cached --stat
git commit -m "Add verified Base evidence history and Pages automation"
git push origin main
```

Workflow ilk push ile çalışmaz: **yalnızca manuel veya zamanlanmış çalıştırma** yapar. İlk testin bilinçli olarak manuel yürütülmesi amaçlanmıştır.

## 3. GitHub Pages ayarı ve ilk canlı kontrol

1. <https://github.com/dogukanerer/equity-evidence-lens/settings/pages> sayfasında `Build and deployment → Source` değerini **GitHub Actions** yapın (eskiden `Deploy from a branch` idi).
2. <https://github.com/dogukanerer/equity-evidence-lens/actions> sayfasından **Equity Evidence - refresh and deploy** akışını seçin; `Run workflow` ile ilk canlı çalışmayı başlatın.
3. İki işin de yeşil olması gerekir: `refresh` (okuma, tarihçeleme, git commit) ve `deploy` (Pages artifact yayını).
4. <https://dogukanerer.github.io/equity-evidence-lens/> üzerinde **Gözlenen arz geçmişi** başlığını ve `data/history.json` bağlantısını kontrol edin. İlk başarılı çalışmada tarihçede **en az 3 gerçek kayıt** beklenir. Eğer yeni blok ilerlememişse veri eklenmeyebilir.
5. Başarıdan sonra yeni otomatik commit Mac'teki repoya gelmediği için bir sonraki kod düzenlemesinden önce `git pull --ff-only` çalıştırın.

**ÖNEMLİ:** GitHub'ın yerleşik `GITHUB_TOKEN`'ı ile üretilen commit, kendi başına `Deploy from a branch` Pages yayını tetiklemez. Bu yüzden workflow veriyi commit ettikten sonra **ayrı bir `upload-pages-artifact` → `deploy-pages`** işi yürütür. Pages Source mutlaka GitHub Actions olmalıdır.

## 4. Arıza olduğunda doğru tepki

- `refresh` kırmızıysa `Actions → failed run → refresh` günlüklerini açın. Eski site yayını korunmuş olmalıdır. Başarısız veriyi `READ_OK` gibi göstermeyin.
- `deploy` kırmızıysa Pages ayarının `GitHub Actions` olduğundan, Pages ortamının izin verdiğinden ve `Actions` işlemlerinin açık olduğundan emin olun. Başarısız yayını önlemek için `refresh` testini atlamayın.
- `SOURCE_MANIFEST_HASH_MISMATCH`: Veri bütünlüğü bozuk. Silip yeniden başlatmayın; Git geçmişi üzerinden inceleyin.
- `INCOMPLETE_NEW_SNAPSHOT_NO_PUBLICATION`: Public RPC bazı sözleşmeleri okuyamadı. Eşik gevşetmek yerine kaynak sorunu incelenmeli.
- RPC'nin geçmiş bloklarda state tutmadığı bir serviste `eth_call` başarısız olabilir; önce düğüm desteği teyit edilmelidir.
- İlk Actions denemesi başarısız olur ve yeni GitHub Actions yayını oluşmazsa Pages ayarını geçici olarak eski `main / (root)` kaynağına geri alabilirsiniz; bu mevcut çalışan siteye dönme yoludur.

## 5. Güvenlik sınırları

- `contents: write` sadece **tarihçe commit eden** job için; `pages: write` + `id-token: write` sadece **yayın** job için. Personal Access Token, cüzdan, seed phrase, API secret gerekmez.
- EVM RPC hostname ve metotları `engine/safe_rpc.py` tarafından allowlist ile sınırlandırılmıştır; imza, yayın, transfer, takas veya emir metotları yok.
- History yalnızca kamusal whitelist verilerini içerir, özel dosyaları kopyalamaz.
- Günlük başına en fazla tek planlanmış örnek hedeflenir; manuel çalıştırma ek kayıt yaratabilir. Bağımsız piyasa gözlemleri değildir.
- `totalSupply()` ve runtime SHA-256 gözlemleri güvenlik sertifikası değildir. Proxy implementation, B20 multiplier, ihraççı sorumlulukları, fiat değer ve yatırım hakları doğrulanmamıştır.
- Airdrop uygunluğu veya Base yarışması ödülü garanti değildir; **yarışma başvurusu/video ayrı işlerdir.**

## 6. Kabul kriterleri

- [ ] Yerelde **76 test** ve `--check-only` başarılı.
- [ ] `git push origin main` başarılı; kaynak GitHub'da görünür.
- [ ] Pages Source = GitHub Actions.
- [ ] İlk manuel `refresh` ve `deploy` işlerindeki tüm kontroller yeşil.
- [ ] `data/history.json` ve üç SVG grafik kamusal sitede açılıyor.
- [ ] İlk canlı yenilemede `data/later.json` daha yeni blok gösteriyor; JSON manifest hash'leri tutuyor.
- [ ] Beklenen günlük çalışmanın ardından yeni commit ve Pages yayını gerçekten görülüyor.

**Kaynak belgeler:**
- <https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages>
- <https://docs.github.com/en/actions/concepts/security/github_token>
- <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows>
- <https://docs.base.org/base-chain/quickstart/connecting-to-base>
