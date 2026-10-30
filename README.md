# LoL Wombo Combo

5'li premade takım için **herkesin gerçekten oynayabildiği** şampiyonlardan wombo combo takımı kurmaya yardım eden araç.

Her arkadaşın için bir **profil** oluşturursun. Bir profile birden fazla Riot hesabı (smurf) bağlanabilir. Program bu hesapların maç geçmişini ve mastery verisini **resmi Riot API**'den çeker ve her profil için bir şampiyon havuzu çıkarır. Sonra o akşam oynayacak 5 kişiyi seçersin, program bir **brief** üretir. Brief'i Claude'a yapıştırırsın, wombo comp'ları Claude kurar. Kurallar brief'in içinde: her oyuncuya yalnızca kendi havuzundan şampiyon verilir, "oynamaz" denen rol verilmez, havuz dışı öneriler açıkça işaretlenir.

## Kurulum

Python 3.10+ yeterli, ek paket gerekmez. Windows'ta Python kurarken "Add to PATH" kutusunu işaretle.

```bash
git clone https://github.com/AliOlkac/LOL_Wombo_Combo.git
```

```bash
cd LOL_Wombo_Combo
```

## Arayüz

```bash
python -m wombo arayuz
```

Windows'ta `baslat.bat` dosyasına çift tıklamak da aynı işi görür. Tarayıcıda `http://127.0.0.1:8765` açılır. Arayüz yalnızca senin bilgisayarından erişilebilir.

1. Sağ üstteki **Anahtar** düğmesinden Riot API anahtarını kaydet. Anahtarı https://developer.riotgames.com adresinden alırsın. Geliştirici anahtarı 24 saatte bir yenilenmeli; süresi geçince düğme sarıya döner.
2. **Profil ekle**: arkadaşın adı ve Riot ID'si (`İsim#TR1`).
3. **Herkesi güncelle**: maç geçmişlerini çeker. İlerleme üstte görünür.
4. **Detay** panelinde şunları yapabilirsin:
   - Rolleri işaretle. Her tıklama sırayla değiştirir: normal → tercih eder ✓ → oynamaz ✕.
   - Not yaz (ör. "tank sever").
   - Smurf hesap ekle.
   - Havuza elle şampiyon ekle (ör. "Wukong, eski main").
5. 5 kişiyi **Takıma ekle** → **Brief oluştur** → **Kopyala** → Claude'a yapıştır.

Anahtar `data/riot_key.txt` dosyasında, veriler `data/wombo.db` dosyasında durur. `data/` klasörü git'e girmez.

## Komut satırı

Arayüzde yapılan her şey komut satırından da yapılabilir:

```bash
python -m wombo anahtar RGAPI-...
python -m wombo ekle Ali "Ali#TR1"            # profil yoksa oluşturur
python -m wombo ekle Ali "AliSmurf#TR1"       # aynı profile ikinci hesap
python -m wombo guncelle                      # herkes (ya da: guncelle Ali Veli)
python -m wombo liste
python -m wombo havuz Ali
python -m wombo rol Ali --tercih top,jg --oynamaz sup --not "tank sever"
python -m wombo ekstra Ali Wukong --seviye esneme --not "eski main"
python -m wombo takim Ali Veli Ayşe Can Ece -o brief.md
```

Sunucu varsayılan olarak `tr1`. Değiştirmek için: `python -m wombo --sunucu euw1 ...`

## Veri çekme süresi

Geliştirici anahtarının limiti 2 dakikada 100 istek. Bir hesabın ilk güncellemesi (100 maç) yaklaşık **2 dakika** sürer. 10 arkadaş için ilk tam güncelleme bu yüzden 20-25 dakika sürebilir. Sonraki güncellemelerde yalnızca yeni maçlar indirilir, birkaç saniyede biter.

## Havuz nasıl hesaplanıyor?

Yalnızca Sihirdar Vadisi maçları sayılır (ARAM ve diğer modlar hariç). Bir profilin tüm hesaplarının maçları birleştirilir, mastery puanları toplanır. Her şampiyona 0-100 arası bir **rahatlık skoru** verilir:

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
