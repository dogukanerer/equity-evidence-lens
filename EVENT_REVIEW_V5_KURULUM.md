# Equity Evidence Lens — Kanıta Dayalı Değişiklik İncelemesi

**Sürüm:** V5 olay analizi yaması, 16 Kasım 2026  
**Kapsam:** V4 ile çalışan `dogukanerer/equity-evidence-lens` deposuna eklenir. Veri toplama, zincir sorgulama, önceki kanıtlar ve mevcut Pages yapılandırması korunur.

## Neden?

V4, ERC-1967/1167 alanlarının tek bloktaki değerini gösterebiliyor. Yeni katman **iki doğrulanmış örnekleme arasında hangi alanların değiştiğini** açık kayıtlar olarak üretir. Hiç işlem göndermez. Önceki V3/V4 doğrulayıcılarını ve SHA-256 manifest kontrolünü kullanır.

### Takip edilen değişiklikler

- Token sözleşmesinin bildirdiği `totalSupply()` artış/azalışı; `decimals` değişiyorsa miktar doğrudan kıyaslanmaz.
- `decimals()` değişikliği, token runtime SHA-256 değişikliği.
- Proxy ERC-1967 implementation/admin/beacon slotlarında adres değişiklikleri.
- Elde edilebildiği ölçüde hedef uygulama adresi, hedef uygulama kod SHA-256'sı, standart proxy türü ve ERC-1167 hedefi değişikliği.

Her olay iki blok numarası, iki blok hash'i, eski/yeni değerler, sabit kimlik ve yorum sınırı içerir. Birden fazla alanın birlikte değişmesi **ayrı gözlemsel bulgular** olabilir; bağımsız güvenlik olayları veya saldırılar değildir. İlk proxy gözlemi tek başına alarm değildir. Kod değişimi güvenlik açığı, arz değişimi mint/burn veya dayanak hisse hakkı kanıtı değildir. Ara bloklardaki değişip geri dönen olaylar kaçabilir. Standart dışı proxy, UUPS yetki sistemi, mint yetkileri ve gerçek faydalanıcı/ihraççı hakları denetlenmez.

## Mac üzerinde güvenli kurulum

0. GitHub Actions çalışması bitmiş olmalı. Terminal'de aynı depoda kontrol et:

```bash
cd ~/Desktop/equity-evidence-lens-site
git status --short
git pull --ff-only
```

`git status --short` çıktı veriyorsa veya `pull` çatışıyorsa devam edip dosyaları ezme. Önce mevcut değişiklikleri incele. `git reset --hard` ve `git push --force` kullanılmaz.

1. ZIP'i Downloads'a indirip bu deponun kök klasörüne çıkart:

```bash
cd ~/Downloads
unzip -n Equity_Lens_V5_Evidence_Events_Patch_2026-11-16.zip -d ~/Desktop/equity-evidence-lens-site
cd ~/Desktop/equity-evidence-lens-site
```

2. GitHub iş akışının bilinen V4 biçiminde olduğundan emin ol, sonra yalnızca bu dosyayı güncelle:

```bash
python3 apply_alert_patch.py --check
python3 apply_alert_patch.py
python3 -m unittest discover -s tests -q
```

Bilinen V4 kaynaklarıyla çevrimdışı testte **124/124** geçti. Senin depoda başka testler varsa toplam sayı farklı olabilir. Çıktıda `OK` olmalı. ZIP'le gelen `apply_alert_patch.py` **site/JSON verilerini değiştirmez**, yalnızca `.github/workflows/equity-evidence-pages.yml` içine güvenli aşamaları ekler.

3. Mac üzerinde ağsız veri ve olay incelemesi:

```bash
python3 scripts/event_review.py --check-only
python3 scripts/event_review.py --preview
```

`--check-only` önceki Base ve proxy JSON/manifest dosyalarını doğrular; `--preview` olayları hesaplar ama **hiçbir dosyaya yazmaz**. `PROXY_HISTORY_NOT_READY`, `SOURCE_MANIFEST_HASH_MISMATCH` veya benzeri hata olursa gönderme/yayımlama yapma.

4. Sadece kod değişikliklerini evvela inceleyip ardından GitHub'a gönder:

```bash
git add apply_alert_patch.py scripts/event_review.py tests/test_event_review.py tests/__init__.py .github/workflows/equity-evidence-pages.yml EVENT_REVIEW_V5_KURULUM.md SHA256_EVENT_PATCH.txt
git diff --cached --stat
git diff --cached -- .github/workflows/equity-evidence-pages.yml
git commit -m "Add evidence-based sampled change review"
git push origin main
```

İlk push'ta yalnızca **kaynak ve iş akışı** gönderilir; yerel `data/` ve `index.html` bu adımla değiştirilmez.

5. [GitHub Actions](https://github.com/dogukanerer/equity-evidence-lens/actions) sayfasına git, `Equity Evidence - refresh and deploy` workflow'unu bir defa çalıştır. `refresh` ve `deploy` başarıyla bitince şunları kontrol et:

- https://dogukanerer.github.io/equity-evidence-lens/#equity-alerts-v1
- https://dogukanerer.github.io/equity-evidence-lens/data/alerts.json
- https://dogukanerer.github.io/equity-evidence-lens/data/alerts_manifest.json

**GitHub Pages kaynağı `GitHub Actions` kalmalı.** Workflow önce mevcut kaynakları doğrular, ardından V3 veri yenileme, V4 proxy kayıt ve **offline olay raporu** üretimi yapar. Ancak tümü başarılıysa değişiklikler commit edilir ve Pages artifact'i yayımlanır. `GITHUB_TOKEN` ile yapılan commit kendi başına Pages tetiklemez; aynı iş akışının `deploy` işi bunu çözer.

## Dosyalar

| Dosya | Ne işe yarıyor? |
|---|---|
| `scripts/event_review.py` | Ağsız, kanıta bağlı olay üretimi, kontrolü ve HTML bölümü |
| `apply_alert_patch.py` | Bilinen V4 workflow'una cerrahi/idempotent ekleme; bilinmeyen layout'ta durur |
| `tests/test_event_review.py` | Sentetik gerçekçi örneklerle veri doğruluğu, uyarı, bütünlük ve HTML güvenliği testleri |
| `tests/__init__.py` | Python test paketinin platformlar arasında tutarlı import edilmesi |
| `data/alerts.json` | Başarılı GitHub iş akışı sırasında üretilir; elle yaratılmaz |
| `data/alerts_manifest.json` | Olay dosyası ve kaynakların hash özeti; EVM state proof değildir |

**Olay yoksa:** "kayıtlı gözlemler arasında gözlenen değişiklik yok" gösterilir. Bu, sözleşmelerin güvenli olduğu veya geçmiş bütün bloklarda değişiklik yaşanmadığı anlamına gelmez.

**Yayına geçiş kriteri:** Testlerin tamamı OK, `--check-only` ve `--preview` başarılı, GitHub `refresh` ve `deploy` yeşil, JSON URL'leri `200`, canlı HTML olay bölümü görünür. Başarısızlıkta mevcut çalışan V4 sitesi korunmalıdır; GitHub Actions hata günlüğünü incele ve sorunu düzeltmeden yeni çalıştırma yapma.

## Bir sonraki ürün kapısı

V5 sonrası gerçek kullanıcı testi (kanıtları anlaşılır buluyor mu?), veri güncelliği göstergesi, test kapsamı ve **5 Aralık 2026** resmî Base yarışma başvurusu. Yarışma yalnızca site yayımlamakla tamamlanmaz: herkese açık X video demosu, `@buildonbase` etiketi ve başvuru formu zorunludur. Resmî kurallar: https://www.base.org/stocksmaxxing/rules. Türkiye'den katılım için ilgili tüm hukukî ve resmî yarışma uygunluk şartları ayrıca kontrol edilmelidir. Token almak, gaz ücreti ödemek veya cüzdan bağlamak zorunlu değildir.
