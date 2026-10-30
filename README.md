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
2. **Kendi profilini ekle** (adın ve Riot ID'n, ör. `Oqila#4310`) ve **Herkesi güncelle**'ye bas. İlerleme üstte görünür.
3. **Önerilen arkadaşlar**: Senin maçlarında aynı takımda en az 3 kez görünen oyuncular burada listelenir. Bunlar büyük ihtimalle premade arkadaşların. Adını yazıp **Ekle**'ye basarsın. Biri zaten ekli bir arkadaşının smurf'üyse, açılır listeden o profili seçersin. Sonra tekrar **Herkesi güncelle**.
4. **Detay** panelinde şunları yapabilirsin:
   - Rolleri işaretle. Her tıklama sırayla değiştirir: normal → tercih eder ✓ → oynamaz ✕.
   - Not yaz (ör. "tank sever").
   - Smurf hesap ekle.
   - Havuza elle şampiyon ekle (ör. "Wukong, eski main").
5. 5 kişiyi **Takıma ekle**, Brief düğmesinin yanından modu seç, **Brief oluştur** → **Kopyala** → Claude ile olan sohbete yapıştır. Claude API anahtarı gerekmez.
   - **Karışık** (varsayılan): yaklaşık üçte biri deneysel
   - **Hepsi sağlam**
   - **Çoğu deneysel (troll ama güçlü):** meta ve lane kalıbı dışı fikirler (iki AP bot, ADC'siz diziliş, mid'de support vb.)

Üstteki **100/200/300 maç** seçimi hesap başına ne kadar geçmiş çekileceğini belirler. Daha derin geçmiş daha çok şampiyon ve kanıtlanmış ikili demek, ama daha uzun sürer.

Brief'te her oyuncu için şunlar yer alır:
- Şampiyon havuzu: her şampiyonun hangi rolde kaç maç oynandığı, takım hasarındaki payı (Hasar%) ve maç başı CC süresi
- Oyun tarzı özeti: hasar payı, alınan hasar payı, CC süresi, kill katılımı, solo kill, CS/dk, görüş/dk
- ARAM'da sık oynadığı şampiyonlar (havuza sayılmaz, deneysel fikirler için)
- Seçili oyuncuların birlikte oynadığı maçlardan kanıtlanmış ikililer (ör. "Ali Miss Fortune (Support) + Lao Jhin (ADC): 14 maç, %71")
- Rol matrisi: her oyuncunun her rolde kaç maç oynadığı ve o rolde en çok hangi şampiyonları oynadığı
- Rank: hesaplar arasından en yüksek Solo ve Flex derecesi (güncelleme sırasında hesap başına tek istekle çekilir; kartlarda da görünür)
- Şampiyon notlarımız: üstteki **Notlar** düğmesinden şampiyonlar hakkında grubun kendi gözlemini yazarsın. Örneğin Locke için varsayılan not: "Çok hızlı sağa sola gidip takım savaşındaki kaosu fırsata çevirir; tek atma ve taşıma potansiyeli yüksek." Şampiyon seçili oyunculardan birinin havuzundaysa not brief'e girer.
- Havuzdaki şampiyonların ulti özetleri ve güncel yama (Data Dragon'dan). Böylece Claude'un bilmediği yeni ya da rework almış şampiyonlar da doğru değerlendirilir. Bu özetler yama başına bir kez, güncelleme sırasında indirilir.

Brief önce veriyi, en sonda kuralları ve çıktı formatını verir. Kurallar şunları içerir:
- Mekanik doğrulama: ör. Yasuo R için havaya kaldırma şart; kanallı ultiler korunmalı
- Az maçlı winrate'e güvenmeme
- Çeşitlilik ve tek bir main'e bağımlılığın sorgulanması

İstenen çıktı kısa ve özdür: tam 5'li comp yerine **10-20 wombo çekirdeği**. Çekirdek, ultileri birbirine zincirlenen 2-5 kişilik (çoğunlukla 3-4) bir gruptur; kalan oyuncular serbesttir ya da isteğe bağlı bir öneri alır. Her çekirdek tek bir tablo satırıdır:

| # | Tip | Çekirdek | Sesli komut | Serbest slotlar | Wombo | Obje | Keyif | Not |
|---|---|---|---|---|---|---|---|---|
| 1 | Sağlam | civaHg Amumu (Jg) + Ali MF (Sup) + Lao Jhin (ADC) | Amumu R → Jhin W → MF R | Top, Mid (isteğe bağlı: Joni Kennen) | 5 | 3 | 4 | ikili 14 maç %71 |

Puanlar 1-5 arası:
- **Wombo:** ulti zincirinin gücü
- **Obje:** ejderha ve Baron'u ne kadar hızlı eritir
- **Keyif:** oynaması ne kadar eğlenceli

Tablonun altında yalnızca öne çıkanlar (en fazla 3 madde), ortak banlar ve varsa belirsizlikler gelir. Bir çekirdeğin ayrıntısını (tam kadro, ilk 15 dakika, draft sırası, yedekler) istemek için numarasını yazman yeterli.

Anahtar `data/riot_key.txt` dosyasında, veriler `data/wombo.db` dosyasında durur. `data/` klasörü git'e girmez.

## Komut satırı

Arayüzde yapılan her şey komut satırından da yapılabilir:

```bash
python -m wombo anahtar RGAPI-...
python -m wombo ekle Ali "Ali#TR1"            # profil yoksa oluşturur
python -m wombo ekle Ali "AliSmurf#TR1"       # aynı profile ikinci hesap
python -m wombo guncelle                      # herkes (ya da: guncelle Ali Veli)
python -m wombo arkadaslar                    # seninle sık oynayanları listele
python -m wombo arkadaslar --ekle 1 --ad Ayşe # listedeki 1 numarayı profil yap
python -m wombo liste
python -m wombo havuz Ali
python -m wombo rol Ali --tercih top,jg --oynamaz sup --not "tank sever"
python -m wombo ekstra Ali Wukong --seviye esneme --not "eski main"
python -m wombo not Locke "kaosu fırsata çevirir, tek atar"     # şampiyon notu (brief'e girer)
python -m wombo not                                             # tüm notlar
python -m wombo takim Ali Veli Ayşe Can Ece -o brief.md
python -m wombo takim Ali Veli Ayşe Can Ece --mod deneysel   # karma | klasik | deneysel
python -m wombo guncelle --mac 300                            # daha derin geçmiş
```

Sunucu varsayılan olarak `tr1`. Değiştirmek için: `python -m wombo --sunucu euw1 ...`

## Veri çekme süresi

Geliştirici anahtarının limiti 2 dakikada 100 istek. Bir hesabın ilk güncellemesi (100 maç) yaklaşık **2 dakika** sürer. 10 arkadaş için ilk tam güncelleme bu yüzden 20-25 dakika sürebilir. Sonraki güncellemelerde yalnızca yeni maçlar indirilir, birkaç saniyede biter.

- **300 maç derinlik:** hesap başına yaklaşık 6 dakika.
- **Ayrıntılı istatistikler:** Bu özellik eklenmeden önce indirilmiş maçlarda ayrıntılı istatistik (hasar payı, CC süresi...) yok. Bu maçlar bir sonraki güncellemede bir kez yeniden indirilir.

## Arkadaş keşfi nasıl çalışıyor?

Her maçta senin takımındaki 4 oyuncunun Riot ID'si ve şampiyonu kaydedilir. Aynı oyuncu son maçlarında 3 veya daha fazla kez yanında çıkıyorsa büyük ihtimalle premade arkadaşındır. Rastgele eşleşen birinin 100 maçta 3 kez yanına düşmesi çok nadirdir.

- Rakip takımdaki oyuncular sayılmaz.
- Aynı maçı iki hesabın da görmüşse (ör. sen ve zaten ekli bir arkadaşın) o maç bir kez sayılır.
- Eklediğin her arkadaşın maçları da taranır. Böylece arkadaşlarının birlikte oynadığı, senin daha az oynadığın kişiler de öneride çıkar.
- Eklenen hesabın sunucusu maç ID'sinden (`TR1_...`) otomatik anlaşılır, mastery verisi doğru sunucudan çekilir.

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
