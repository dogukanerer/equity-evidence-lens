# Equity Evidence Lens V4 — Proxy / implementation kanıtı

**15 Kasım 2026 — yerel paket.** Bu paket *mevcut yayımlanmış* V3 projesine cerrahi bir eklemedir; yeni repo açmaz. **Canlı V4 RPC ve GitHub Actions yayını henüz Mac/GitHub'da denenmedi.** Paket bilinen V3 kaynakları üzerine uygulanıp **104 yerel testle** doğrulandı (76 korunmuş + 28 yeni). Yerel testler sentetik yanıtlar kullanır, gerçek borsa/akıllı sözleşme güvenliği kanıtı değildir.

## Neyi araştırır?

- Aynı Base Mainnet blok numarası ve hash'inde üç token için runtime kodu; V3 kaydıyla SHA-256 karşılaştırması.
- **ERC-1967** `implementation`, `admin`, `beacon` depolama alanlarının ham 32-byte değerleri ve 20-byte adresleri.
- `beacon` örneğinde beacon sözleşmesi için `implementation()` salt-okunur çağrısı (yalnızca implementation slot boşsa ve göstergeler çelişmiyorsa).
- **Yalnızca** ERC-1167'nin tam 45-byte *kanonik* minimal proxy biçimi (alternatif ve genişletilmiş clone biçimleri kapsam dışıdır).
- Saptanan hedef uygulama sözleşmesinin runtime kod SHA-256 değeri. Kodun kaynağını veya güvenliğini doğrulamaz.
- İki başarılı denetim arasında adres, depo alanı veya kod özetinin değişmesi durumunda **manuel inceleme** uyarısı. Aradaki bloklarda olup bitenler bilinmez.

**Çok önemli:** Boş ERC-1967 alanı = "yetki yok" **değildir**. UUPS yetkileri, `Ownable`, `AccessControl`, multisig, yükseltme onay mekanizmaları, diamond/custom proxy'ler, B20 çarpanı, token hakları, ihraççı durumu veya yatırım güvenliği bu sürümle incelenmez. Çakışan adres göstergeleri varsa "kesin uygulama" iddiası yapılmaz. Veri, halka açık RPC'de okunan bilgilerdir; kriptografik Merkle/state kanıtı değildir.

## Ön koşul

Mevcut `~/Desktop/equity-evidence-lens-site` deposunda V3 kodları bulunmalı (`scripts/refresh_site.py`, `engine/safe_rpc.py`, `.github/workflows/equity-evidence-pages.yml`). Sitenin GitHub Actions ile yayınlandığı daha önce doğrulandı. Yeni otomatik commit'lerin Mac'ine alınması gerekir.

### 1. Önce uzaktaki güncel veriyle eşitle

```bash
cd ~/Desktop/equity-evidence-lens-site
git status --short
git pull --ff-only
```

`git status` başka değişiklikler gösteriyorsa incele; zorla sıfırlama yapma. `git pull --ff-only` çakışma verirse DUR ve çıktıyı inceleyelim. `git push --force` kullanma.

### 2. ZIP'i yükle, uyumluluğu kontrol et ve uygula

ZIP'i `Downloads` içine indirdikten sonra:

```bash
cd ~/Downloads
unzip -n Equity_Lens_V4_Proxy_Evidence_Patch_2026-11-15.zip -d ~/Desktop/equity-evidence-lens-site
cd ~/Desktop/equity-evidence-lens-site

python3 apply_proxy_patch.py --check
python3 apply_proxy_patch.py
python3 -m unittest discover -s tests -q
python3 scripts/refresh_site.py --check-only
python3 scripts/proxy_monitor.py --check-only
```

`apply_proxy_patch.py`, mevcut `engine/safe_rpc.py` ve GitHub Actions workflow'unu **bilinen V3 işaretleri eşleşirse** değiştirir; farklı kaynak gördüğünde işleme devam etmez. **Sitedeki mevcut JSON/HTML dosyalarını değiştirmez.** Tekrar çalıştırılırsa daha önce kurulmuş V4'ü algılar.

Beklenen `Ran 104 tests`, `OK`; son kontrol `PROXY_LOCAL_VALIDATION_NO_NETWORK`, başlangıçta `proxy_history: 0`, `written: false`. Mevcut repoda V4 zaten kurulmuşsa `proxy_history` sıfır olmayabilir.

### 3. Kodları GitHub'a gönder

Sadece testler başarılıysa:

```bash
git add apply_proxy_patch.py scripts/proxy_monitor.py tests/test_proxy_monitor.py engine/safe_rpc.py .github/workflows/equity-evidence-pages.yml PROXY_V4_KURULUM_VE_DENETIM.md

git diff --cached --stat
git commit -m "Add pinned proxy implementation evidence checks"
git push origin main
```

`git diff --cached --stat` içinde `data/history.json`, `index.html` gibi otomatik üretilen kayıtları **yerel kurulum aşamasında** görmemelisin. Bunları bilerek güncellemedik; ilk canlı çalışma GitHub Actions üzerinden yapılacak.

### 4. İlk canlı çalışmayı bilinçli olarak başlat

- <https://github.com/dogukanerer/equity-evidence-lens/actions> adresini aç.
- `Equity Evidence - refresh and deploy` → `Run workflow` seç.
- `refresh` aşamasında sırasıyla eski veri doğrulaması, yeni Base gözlemi, proxy kanıtları, commit, Pages artifact oluşturma; sonra `deploy` aşamasının yeşil olması gerekir.
- <https://dogukanerer.github.io/equity-evidence-lens/> sayfasında **"Proxy ve uygulama kanıtları"** bölümünün açıldığını doğrula. `data/proxy_audit.json`, `data/proxy_history.json`, `data/proxy_manifest.json` bağlantıları çalışmalı.

**İlk gün sadece bir proxy temel gözlemi elde edilir.** Bir değişiklik olduğunu iddia etmek için daha sonra alınmış ikinci başarılı gözlem gerekir. Günlük iş mevcut V3 takvimini kullanır; gecikme ve atlama olasılığı vardır.

### 5. Başarısız olursa

- `PROXY_PATCH_STOPPED`: V3 kaynak yapısı beklenenden farklı; üzerine zorla yazma.
- `V3_SNAPSHOT_TOO_OLD_REFRESH_FIRST`: Yeni ve yeterince yakın zamanda alınmış V3 kaydı lazım. Kodun zaman denetimini gevşetme.
- `RPC returned an error` veya HTTP hata/limit: Public Base RPC sınırlı olabilir. Siteye yeni veri yazılmadan çalışma durur. Gerekirse aynı workflow'u tekrar çalıştırıp günlükleri karşılaştır.
- `RUNTIME_CODE_MISMATCH...`: Aynı blokta V3 ve proxy denetimi farklı bytecode gördü; bu gözlemi yayımlamayız.
- `PROXY_MANIFEST_DIGEST_MISMATCH`: Mevcut kanıt dosyası bütünlüğü değişmiş; incelemeden manifesti yenileme.
- `refresh` veya `deploy` kırmızıysa mevcut başarılı GitHub Pages sitesi korunmalıdır; yeni bir yayın başarılı oldu varsayma.

### Neden V3 dosyalarını doğrudan değiştirmeden ekliyoruz?

- V3'ün `refresh_site.py` raporunu yeniden oluşturması, daha sonra çalışan V4 denetimini silmesin diye V4 **her yenilemeden sonra** devreye girer.
- V4 başarısızsa otomatik commit ve Pages deploy aşamaları yürütülmez.
- V4, eski V3 tarihçesinden önceki proxy durumunu **uydurmaz**; değişiklik takibi V4'ün ilk kanıtından başlar.
- Para, kripto cüzdanı, API anahtarı veya işlem gönderme yetkisi gerekli değildir.

### Doğrudan standart kaynakları

- ERC-1967: <https://eips.ethereum.org/EIPS/eip-1967>
- ERC-1167: <https://eips.ethereum.org/EIPS/eip-1167>
- OpenZeppelin proxy sınırlamaları: <https://docs.openzeppelin.com/contracts/5.x/api/proxy>
- GitHub Pages custom workflow: <https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site>
- Base RPC: <https://docs.base.org/base-chain/quickstart/connecting-to-base>
