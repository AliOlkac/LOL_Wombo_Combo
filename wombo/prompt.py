"""Takım havuzlarından Claude'a verilecek wombo combo brief'ini üretir.

Yapı: kısa görev tanımı → veriler (oyuncular, rol matrisi, ortak maçlar, ulti özetleri) →
kurallar ve çıktı formatı. Uzun veri önce, talimatlar en sonda: model talimatları veriyi
okuduktan sonra görür.
"""
import re

from .pool import FLEX_BREADTH, MAIN, PLAYABLE, POSITION_NAMES, STRETCH
from .store import POSITIONS, _norm
from .synergy import duo_stats, pair_totals

MAX_CHAMPS = 25       # oyuncu başına brief'e girecek en fazla şampiyon
MAX_DUOS = 15         # brief'e girecek en fazla şampiyon ikilisi
MAX_GROUP_GAMES = 8   # en az 3 kişiyle oynanmış son maçlardan kaç tanesi listelensin
KIT_DESC_LIMIT = 220  # ulti açıklaması en fazla kaç karakter

LEGEND = (
    f"Seviye maç sayısı, winrate, KDA, yakın zamanda oynanma ve mastery'den hesaplandı: "
    f"**{MAIN}** rahat oynar, **{PLAYABLE}** makul oynar, **{STRETCH}** zorlanabilir. "
    f"**Esnek** = en az {FLEX_BREADTH} {MAIN}/{PLAYABLE} şampiyonu olan oyuncu. "
    "Roller sütunu: o şampiyonla hangi rolde kaç SR maçı oynandı."
)

MODES = {
    "karma": "2 sağlam + 1 deneysel",
    "klasik": "3 sağlam",
    "deneysel": "3 deneysel (troll görünen ama güçlü)",
}

EXPERIMENTAL = """
## Deneysel comp ne demek
Lane ve meta kalıplarına sıkışma. "Troll görünür ama mekanik olarak tutarlı" fikirler istiyoruz:
iki AP bot, ADC'siz comp, iki support, mid'de ADC ya da support şampiyonu, top'ta ranged ya da
support şampiyonu, erken 5'li baskın, funnel, lane swap gibi. Şampiyonlar yine havuzdan gelir
(Esnek oyunculara işaretli havuz dışı öneri serbest; ARAM'da sık oynadıkları da aday olabilir).
Her deneysel comp için yaz:
- Neden işe yarar, rakip neyi beklemez
- Hangi rakip comp'a karşı açılmamalı
- Oyuncu bu şampiyonu bu rolde daha önce oynamış mı (Roller sütunu), oynamadıysa riski
Oyun tarzı satırlarını (hasar payı, alınan hasar, CC süresi, solo kill) kimin taşıyıcı, kimin
ön saf/engage olabileceğine karar verirken kullan.
"""


def _check_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Bilinmeyen mod: {mode}. Seçenekler: {', '.join(MODES)}")


def task_intro(mode="karma", patch=None):
    """Brief'in başı: rol, amaç ve bağlam. Ayrıntılı kurallar verinin ardından gelir."""
    _check_mode(mode)
    if mode == "klasik":
        task = "3 farklı sağlam wombo comp kur."
    elif mode == "karma":
        task = "3 farklı wombo comp kur: ilk ikisi sağlam, **üçüncüsü deneysel**."
    else:
        task = "3 farklı **deneysel** wombo comp kur."
    source = f"Riot API, yama {patch}" if patch else "Riot API"
    return f"""\
# Wombo combo koçluk isteği

Sen deneyimli bir League of Legends koçu ve analistisin. Türkçe cevap ver.
Aşağıda 5 kişilik premade takımımızın gerçek maç verisi var ({source}).
Bu 5 kişiyle oynanabilecek **wombo combo** takımları kur: takım savaşında üst üste binen AoE
engage, havaya kaldırma/çekme ve AoE hasar ultilerinin zincirlendiği diziliş. {task}

Hepimiz aynı takımda ve sesli iletişimdeyiz: solo queue'da tutmayan, zamanlama isteyen ulti
zincirleri bizim için gerçekçi. Önce verileri oku; kurallar ve istenen çıktı formatı en sonda.
"""


def rules_and_format(mode="karma"):
    """Brief'in sonu: kurallar, mekanik doğruluk, çeşitlilik ve çıktı formatı."""
    _check_mode(mode)
    stretch_limit = "iki" if mode == "deneysel" else "bir"
    experimental = EXPERIMENTAL if mode != "klasik" else ""
    text = f"""\
# Kurallar

## Havuz ve roller
- Her oyuncuya yalnızca **kendi havuzundaki** şampiyonlardan ver.
  Tercih sırası: {MAIN} > {PLAYABLE} > {STRETCH}. Comp başına en fazla {stretch_limit} {STRETCH} pick olsun.
- Her rol (Top, Jungle, Mid, ADC, Support) bir kez. **Meta rol kalıplarına takılma:** "Roller"
  sütunu ve rol matrisi oyuncunun şampiyonu gerçekte hangi rolde kaç maç oynadığını gösterir
  (ör. Kai'Sa Mid, Miss Fortune Support). Şampiyonu oyuncunun oynadığı rolde değerlendir;
  off-meta seçimler serbest, iyi çalışıyorsa tercih sebebidir.
- **"Oynamaz" (✕) rol o oyuncuya asla verilmez.** "Tercih eder" (✓) rolleri mümkünse kullan.
- **Esnek** işaretli oyunculara havuzlarında olmayan ama oynadıklarına çok benzeyen bir şampiyon
  önerebilirsin; mutlaka "havuz dışı öneri" diye işaretle ve neden benzediğini söyle.

## Veriyi doğru oku
- Az maç gürültüdür: 5 maçtan az winrate'i kanıt sayma; maç sayısı ve mastery daha güvenilir.
  Maç sayısı 0 olan (yalnızca mastery'den gelen) şampiyonlar uzun süredir oynanmamış olabilir:
  kullanırsan "pas atmış olabilir" riskini belirt.
- "Hasar%" takım hasarındaki pay, "CC sn" maç başı rakibi kontrol ettiği süre. Oyun tarzı
  satırlarıyla birlikte kimin taşıdığını, kimin ön saf/engage olduğunu bunlardan oku.
- **Birlikte oynadığınız maçlar** gerçek ortak maçlarımızdan çıkarıldı. Çok maçlı ve yüksek
  winrate'li ikililer kanıtlanmış silahlarımızdır: en az bir comp'u bunlardan birinin etrafına kur
  ve onu güçlendirecek (engage, AoE, koruma) parçaları ekle. 2-3 maçlık ikililere fazla güvenme.

## Mekanik doğruluk
- Her combo adımının gerçekten çalıştığını kontrol et. Örnekler: Yasuo R yalnızca havaya kalkmış
  ya da savrulmuş hedeflere basılır (stun veya kök yetmez); kanallı ultiler (Miss Fortune,
  Katarina, Fiddlesticks, Malzahar...) CC ile kesilir, kilit süresine sığmalı ya da korunmalı;
  gecikmeli yetenekler (Cho'Gath Q, Lux R) önce kilitlenmiş hedefe atılmalı.
- Emin olmadığın bir etkileşimi kesin gibi yazma; "emin değilim" de.
- Bilmediğin, yeni çıkmış ya da rework almış şampiyonlarda **Şampiyon ulti özetleri** bölümünü
  esas al. Yine de emin değilsen o şampiyonu combo'nun kilit parçası yapma ve sonda sor.

## Çeşitlilik
- Comp'lar birbirinin kopyası olmasın: her biri farklı bir engage kaynağına ya da kazanma koşuluna
  dayansın.
- Bir oyuncunun tek gerçek main'i her comp'un kilidi oluyorsa bu bir zafiyettir (banlanırsa hepsi
  çöker): en az bir comp'u o şampiyon olmadan kur ve bunu belirt.
{experimental}
# İstenen çıktı
Kısa ve uygulanabilir yaz; genel LoL bilgisi anlatma, bizim verimize dayan.

1. **Özet tablo** (en üstte): | Comp | Tip (sağlam/deneysel) | Top | Jungle | Mid | ADC | Support | Güç zamanı |
2. Her comp için:
   - Başlık: akılda kalan bir isim + tek cümlelik fikir
   - Kadro tablosu: | Oyuncu | Rol | Şampiyon | Seviye | Veriden kanıt | (kanıt: maç/WR, Hasar%,
     CC sn ya da kanıtlanmış ikili)
   - Combo: numaralı adımlar ve takım savaşında söylenecek tek satırlık **sesli komut**
     (ör. "Amumu R → Lux R → MF R")
   - İlk 15 dakika: hangi koridor öncelikli, jungle hangi tarafa oynar, ilk objektif planı
   - Takım savaşı planı ve güç zamanı (erken / orta / geç)
   - Zayıf yönler ve en fazla 3 ban önerisi
   - Yedek pickler: kilit şampiyon banlanırsa oyuncu → yedek şampiyon
3. **Draft planı:** ilk turda combo'yu belli etmeyen pickler, son pickler, rakip şunu alırsa hangi
   comp'a geçilir.
4. **Bu akşamın önerisi:** hangi comp'la başlamalıyız ve neden (tek paragraf).
5. **Antrenman:** Esneme ya da uzun süredir oynanmayan picklerden kim neyi önce normal maçta denemeli.
6. **Belirsizlikler:** emin olmadığın şampiyon, etkileşim ya da eksik veri varsa soru olarak listele.

Draft sırasında rakip pickleri yazarsak comp'u onlara göre uyarlamaya hazır ol.
"""
    return text


def _pct(x):
    return f"%{round(x * 100)}"


def _roles(roles):
    return ", ".join(POSITION_NAMES.get(r, r) for r in roles)


def _champ_role(champ_names, champion, position):
    name = champ_names.get(_norm(champion or ""), champion or "?")
    return f"{name} ({POSITION_NAMES.get(position, position)})" if position else name


def style_line(style):
    """Oyuncunun SR maç başı ortalamalarını tek satırda anlatır."""
    parts = []
    if "dmg_share" in style:
        parts.append(f"takım hasarının {_pct(style['dmg_share'])}'i")
    if "taken_share" in style:
        parts.append(f"alınan hasarın {_pct(style['taken_share'])}'i")
    if "cc_time" in style:
        parts.append(f"{style['cc_time']:.0f} sn CC")
    if "kp" in style:
        parts.append(f"KP {_pct(style['kp'])}")
    if "solo_kills" in style:
        parts.append(f"{style['solo_kills']:.1f} solo kill")
    if "cs_pm" in style:
        parts.append(f"{style['cs_pm']:.1f} CS/dk")
    if "vision_pm" in style:
        parts.append(f"{style['vision_pm']:.1f} görüş/dk")
    return " · ".join(parts)


def player_section(pool, names):
    title = f"### {pool.name}"
    if pool.riot_ids:
        title += f" ({', '.join(pool.riot_ids)})"
    if pool.flexible:
        title += " — Esnek"
    share = ", ".join(f"{POSITION_NAMES.get(p, p)} {_pct(s)}" for p, s in pool.role_share.items())
    lines = [title, f"Son {pool.sr_games} Sihirdar Vadisi maçı. Rol dağılımı: {share or 'veri yok'}"]
    if pool.preferred_roles:
        lines.append(f"Tercih ettiği roller: {_roles(pool.preferred_roles)}")
    if pool.avoided_roles:
        lines.append(f"Oynamaz: {_roles(pool.avoided_roles)}")
    if pool.note:
        lines.append(f"Not: {pool.note}")
    style = style_line(pool.style)
    if style:
        lines.append(f"Oyun tarzı (SR maç başı ortalama): {style}")
    aram = [f"{names.get(_norm(c), c)} {n}" for c, n in pool.aram if n >= 2][:10]
    if aram:
        lines.append(f"ARAM'da oynadıkları (havuza sayılmaz, deneysel fikir için): {', '.join(aram)}")
    lines.append("")
    if not pool.champions:
        lines.append("_Havuz boş: maç verisi yok._")
        return "\n".join(lines)
    lines += ["| Şampiyon | Seviye | Maç | WR | KDA | Roller | Hasar% | CC sn | Mastery | Not |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for c in pool.champions[:MAX_CHAMPS]:
        name = names.get(_norm(c.champion), c.champion)
        pos = ", ".join(f"{POSITION_NAMES.get(p, p)} {n}" for p, n in c.positions.most_common(3)) or "-"
        if c.games:
            wr, kda = _pct(c.winrate), f"{c.kda:.1f}"
        else:
            wr = kda = "-"
        mastery = f"{c.mastery_points // 1000}k" if c.mastery_points else "-"
        note = c.note or ("elle eklendi" if c.extra_tier else "")
        avg = c.style.averages()
        dmg = _pct(avg["dmg_share"]) if "dmg_share" in avg else "-"
        cc = f"{avg['cc_time']:.0f}" if "cc_time" in avg else "-"
        lines.append(f"| {name} | {c.tier} | {c.games} | {wr} | {kda} | {pos} | {dmg} | {cc} | {mastery} | {note} |")
    return "\n".join(lines)


def role_matrix_section(pools, names):
    """Oyuncu × rol: o roldeki SR maç sayısı ve en çok oynanan şampiyonlar."""
    header = " | ".join(POSITION_NAMES[r] for r in POSITIONS)
    lines = ["## Rol matrisi",
             "Her hücre: o roldeki SR maç sayısı · o rolde en çok oynadığı şampiyonlar. "
             "✓ tercih eder, ✕ oynamaz.", "",
             f"| Oyuncu | {header} |", "|---" * (len(POSITIONS) + 1) + "|"]
    for pool in pools:
        cells = []
        for role in POSITIONS:
            games = pool.role_counts.get(role, 0)
            champs = ", ".join(names.get(_norm(c), c) for c, _ in pool.role_champs.get(role, []))
            cell = f"{games} · {champs}" if games else "-"
            if role in pool.avoided_roles:
                cell = f"✕ {cell}"
            elif role in pool.preferred_roles:
                cell = f"✓ {cell}"
            cells.append(cell)
        lines.append(f"| {pool.name} | {' | '.join(cells)} |")
    return "\n".join(lines)


def synergy_section(matches, profile_names, champ_names):
    """Seçili oyuncuların birlikte oynadığı SR maçlarından ikili ve kadro özeti."""
    lines = ["## Birlikte oynadığınız maçlar", ""]
    if not matches:
        lines.append("_Seçili oyuncuların aynı takımda oynadığı Sihirdar Vadisi maçı bulunamadı._")
        return "\n".join(lines)
    lines += [f"Seçili oyunculardan en az ikisinin aynı takımda olduğu {len(matches)} Sihirdar Vadisi maçı.",
              "", "### Oyuncu ikilileri (şampiyondan bağımsız)"]
    for (a, b), (games, wins) in pair_totals(matches):
        lines.append(f"- {profile_names[a]} + {profile_names[b]}: {games} maç, {_pct(wins / games)}")

    duos = duo_stats(matches)[:MAX_DUOS]
    lines += ["", "### Şampiyon ikilileri (en az 2 maç)"]
    if duos:
        lines += ["| İkili | Maç | WR |", "|---|---|---|"]
        for (a, ca, ra, b, cb, rb), games, wins in duos:
            lines.append(f"| {profile_names[a]} {_champ_role(champ_names, ca, ra)} + "
                         f"{profile_names[b]} {_champ_role(champ_names, cb, rb)} | {games} | {_pct(wins / games)} |")
    else:
        lines.append("_Aynı şampiyon ikilisiyle 2+ maç yok._")

    groups = [m for m in matches if len(m.players) >= 3][:MAX_GROUP_GAMES]
    if groups:
        lines += ["", "### Son ortak maçlar (en az 3 kişi)"]
        for m in groups:
            members = ", ".join(f"{profile_names[pid]} {_champ_role(champ_names, c, r)}"
                                for pid, (c, r) in sorted(m.players.items(), key=lambda kv: profile_names[kv[0]].lower()))
            lines.append(f"- {'Galibiyet' if m.win else 'Mağlubiyet'} · {members}")
    return "\n".join(lines)


def clean_kit_text(text, limit=KIT_DESC_LIMIT):
    """Data Dragon açıklamasındaki HTML etiketlerini temizler ve kelime sınırında kısaltır."""
    text = re.sub(r"<br\s*/?>", " ", text or "", flags=re.IGNORECASE)
    text = " ".join(re.sub(r"<[^>]+>", "", text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def kits_section(kits, names, patch=None):
    """Havuzlardaki şampiyonların ulti özetleri: bilinmeyen/yeni şampiyonlar için dayanak."""
    if not kits:
        return ""
    title = f"## Şampiyon ulti özetleri (Data Dragon{', yama ' + patch if patch else ''})"
    lines = [title, "Bilmediğin ya da emin olmadığın şampiyonlarda bunu esas al.", ""]
    for ddid, kit in sorted(kits.items(), key=lambda kv: names.get(_norm(kv[0]), kv[0]).lower()):
        name = names.get(_norm(ddid), ddid)
        tags = f" [{', '.join(kit['tags'])}]" if kit.get("tags") else ""
        lines.append(f"- **{name}**{tags} — R {kit['ult_name']}: {clean_kit_text(kit['ult_desc'])}")
    return "\n".join(lines)


def build_prompt(pools, names, shared=None, profile_names=None, mode="karma", kits=None, patch=None):
    """shared: synergy.shared_matches çıktısı; profile_names: profil id -> ad; mode: MODES anahtarı;
    kits: ddragon_id -> ulti özeti; patch: Data Dragon sürümü."""
    parts = [task_intro(mode, patch), "## Oyuncular", "", LEGEND, ""]
    parts += [player_section(p, names) + "\n" for p in pools]
    parts.append(role_matrix_section(pools, names) + "\n")
    if shared is not None:
        parts.append(synergy_section(shared, profile_names, names) + "\n")
    kit_text = kits_section(kits, names, patch)
    if kit_text:
        parts.append(kit_text + "\n")
    parts.append(rules_and_format(mode))
    return "\n".join(parts)
