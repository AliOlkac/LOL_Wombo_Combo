# LoL Wombo Combo

5'li premade takım için **herkesin gerçekten oynayabildiği** şampiyonlardan wombo combo takımı kurmaya yardım eden araç.

Program arkadaşlarının maç geçmişini ve mastery verisini **resmi Riot API**'den çeker. Her oyuncu için bir şampiyon havuzu hesaplar ve 5 kişilik takım için bir **brief** üretir. Bu brief'i Claude'a verirsin, wombo comp'ları Claude kurar. Kurallar brief'in içinde: her oyuncuya yalnızca kendi havuzundan şampiyon verilir, havuz dışı öneriler açıkça işaretlenir.

## Kurulum

Python 3.10+ yeterli, ek paket gerekmez.

1. Riot geliştirici anahtarını https://developer.riotgames.com adresinden al. Bu anahtar 24 saatte bir yenilenir. Kalıcı kullanım için oradan ücretsiz "Personal API Key" başvurusu yapılabilir.
2. Anahtarı ortam değişkeni olarak ver. Anahtarı repoya yazma.

```bash
export RIOT_API_KEY="RGAPI-..."
```

Windows PowerShell için: `$env:RIOT_API_KEY="RGAPI-..."`

## Kullanım

```bash
python -m wombo ekle "Arkadas#TR1" --ad ali       # oyuncu ekle
python -m wombo guncelle                          # herkesin son 100 maçını + mastery'sini çek
python -m wombo liste
python -m wombo havuz ali                         # ali'nin havuzunu gör
python -m wombo ekstra ali Wukong --seviye esneme --not "eski main"   # elle şampiyon ekle
python -m wombo takim ali veli ayse can ece -o brief.md               # Claude brief'i üret
```

`brief.md` içeriğini Claude'a yapıştır. Claude 3 farklı wombo comp, combo sırası, kazanma planı, ban önerileri ve yedek pick'ler döner.

Seçenekler:
- `--sunucu`: varsayılan `tr1`. Örnek: `python -m wombo --sunucu euw1 ...`
- `guncelle --mac 200`: oyuncu başına daha fazla maç çeker.

Yalnızca yeni maçlar indirilir, veriler `data/wombo.db` dosyasında saklanır. Geliştirici anahtarının hız limiti (2 dakikada 100 istek) yüzünden ilk güncelleme oyuncu başına birkaç dakika sürebilir.

## Havuz nasıl hesaplanıyor?

Yalnızca Sihirdar Vadisi maçları sayılır (ARAM ve diğer modlar hariç). Her şampiyona 0-100 arası bir **rahatlık skoru** verilir:

| Bileşen | Ağırlık | Not |
|---|---|---|
| Deneyim (maç sayısı) | 40 | Azalan getiriyle artar |
| Performans (winrate + KDA) | 20 | Deneyimle çarpılır; winrate az maçta %50'ye doğru düzeltilir |
| Yakın zamanda oynanma | 15 | Deneyimle çarpılır |
| Mastery puanı | 25 | Logaritmik ölçek |

Skora göre seviyeler:
- **Main**: 60 ve üstü
- **Oynayabilir**: 40 ve üstü
- **Esneme**: 20 ve üstü

Son maçlarda hiç oynanmamış ama mastery'si yüksek şampiyonlar (eski mainler) de havuza girer:
- 100k üstü: Oynayabilir
- 30k üstü: Esneme

Main + Oynayabilir şampiyonu 8 veya daha fazla olan oyuncular **Esnek** sayılır. Claude bu oyunculara havuzlarında olmayan ama oynadıklarına benzeyen şampiyonlar da önerebilir.

## Testler

```bash
python -m unittest discover -s tests -t .
```
