# WiFi Konum

MacBook'un WiFi sinyallerini kullanarak ev içinde kabaca konum tahmini ve hareket algılama yapan küçük bir deneme projesi.

## Ne yapar

- **Plan:** Odanın planını dikdörtgen alanlar olarak çizersin.
- **Ölçüm:** MacBook'la odada dolaşıp haritada durduğun yere tıklarsın; her noktanın WiFi "parmak izi" kaydedilir ve sinyal haritası oluşur.
- **Canlı:** Kayıtlı parmak izlerine bakarak MacBook'un nerede olduğunu tahmin eder (ızgara tabanlı Bayes filtresi).
- **Hareket:** MacBook sabit dururken, bağlı ağın sinyalindeki dalgalanmadan odada hareket olup olmadığını söyler.

## Çalıştırma

Gerekenler: macOS, Python 3, Xcode komut satırı araçları (`swift` komutu için).

```bash
python3 server.py
```

Sonra tarayıcıda `http://localhost:8765` adresini aç. Finder'dan `baslat.command` dosyasına çift tıklamak da aynı işi yapar.

## Sınırlar

- macOS tam WiFi taramasına yaklaşık 13 saniyede bir izin verir; konum yavaş güncellenir.
- Doğruluk oda/bölge seviyesindedir (kabaca 1–2 metre), GPS gibi hassas değildir.
- Konumu gösterilen şey MacBook'tur; iPhone WiFi taramasına izin vermez.
- Hareket algılama yalnızca "var / yok" der, konum vermez.

## Dosyalar

| Dosya | Görev |
|---|---|
| `wifiscan.swift` | CoreWLAN ile sinyalleri okur |
| `server.py` | Yerel sunucu, ölçümleri saklar, hareket algılar |
| `locate.py` | Sinyal haritası ve konum filtresi |
| `simulate.py` | Algoritmayı sahte bir dairede dener |
| `index.html` | Arayüz |

Ölçümler `data.json` dosyasında yalnızca kendi bilgisayarında tutulur ve depoya eklenmez.
