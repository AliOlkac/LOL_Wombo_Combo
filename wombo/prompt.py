"""Takım havuzlarından Claude'a verilecek wombo combo brief'ini üretir.

Yapı: kısa görev tanımı → veriler (oyuncular, rol matrisi, ortak maçlar, ulti özetleri) →
kurallar ve çıktı formatı. Uzun veri önce, talimatlar en sonda: model talimatları veriyi
okuduktan sonra görür.
"""
import re

from . import __version__

from .pool import FLEX_BREADTH, MAIN, PLAYABLE, POSITION_NAMES, STRETCH, format_rank
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
    "karma": "karışık (yaklaşık üçte biri deneysel)",
    "klasik": "hepsi sağlam",
    "deneysel": "çoğu deneysel (troll görünen ama güçlü)",
}
CORE_COUNT = "15-20"  # tek seferde istenen wombo çekirdeği sayısı
MIN_CORES = 15

EXPERIMENTAL = """
## Deneysel öneri ne demek
Lane ve meta kalıplarına sıkışma: "troll görünür ama mekanik olarak tutarlı" fikirler. Örnekler:
iki AP bot, ADC'siz diziliş, iki support, mid'de ADC ya da support şampiyonu, top'ta ranged ya da
support şampiyonu, erken 5'li baskın, funnel, lane swap. Şampiyonlar yine havuzdan gelir (Esnek
oyunculara işaretli havuz dışı öneri ve ARAM'da sık oynadıkları da aday). Not sütununda neden
işe yaradığını ya da riskini kısaca yaz.
"""


def _check_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Bilinmeyen mod: {mode}. Seçenekler: {', '.join(MODES)}")


MAX_NOTE = 1500  # özel notun en fazla karakter sayısı


def special_note_section(note):
    """Kullanıcının bu brief için yazdığı serbest not: brief'in en üstüne, alıntı olarak."""
    note = (note or "").strip()[:MAX_NOTE]
    if not note:
        return ""
    quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in note.splitlines())
    return f"## Özel notumuz (bu seferki öncelik)\n\n{quoted}\n\n"


def task_intro(mode="karma", patch=None, note=None):
    """Brief'in başı: rol, amaç ve bağlam. Ayrıntılı kurallar verinin ardından gelir."""
    _check_mode(mode)
    mix = {"klasik": "Hepsi sağlam olsun.",
           "karma": "Yaklaşık üçte biri **deneysel** olsun.",
           "deneysel": "Çoğu **deneysel** olsun (troll görünen ama güçlü)."}[mode]
    source = f"Riot API, yama {patch}" if patch else "Riot API"
    return f"""\
# Wombo combo koçluk isteği

{special_note_section(note)}Sen deneyimli bir League of Legends koçu ve analistisin. Türkçe, **kısa ve öz** cevap ver.
Aşağıda 5 kişilik premade takımımızın gerçek maç verisi var ({source}).
Bize **{CORE_COUNT} farklı öneri** ver (en az {MIN_CORES}; 3-4 comp ile yetinme): **yaklaşık
yarısı tam 5'li comp**, kalanı 3-4 kişilik wombo çekirdeği. {mix}

Hepsi wombo combo üzerine kurulu olsun: ultilerin ve CC'lerin birbirine zincirlendiği AoE engage,
havaya kaldırma/çekme ve AoE hasar.
- **5'li comp:** beş oyuncunun da rolü ve şampiyonu belli; beşi de aynı zincire hizmet eder.
- **Wombo çekirdeği (3-4 kişi):** zincirin kalbi; çekirdekte olmayan oyuncular serbesttir, kendi
  rollerinde rahat ettikleri şampiyonu alırlar.

Hepimiz aynı takımda ve sesli iletişimdeyiz: zamanlama isteyen ulti zincirleri bizim için gerçekçi.
Önce verileri oku; kurallar ve istenen çıktı formatı en sonda.
"""


def rules_and_format(mode="karma", has_note=False):
    """Brief'in sonu: kurallar, mekanik doğruluk, çeşitlilik ve kısa çıktı formatı."""
    _check_mode(mode)
    stretch_limit = "iki" if mode == "deneysel" else "bir"
    experimental = EXPERIMENTAL if mode != "klasik" else ""
    note_rule = ("\n**En üstteki özel not bu seferki isteğimizdir; aşağıdaki kurallarla çelişirse "
                 "özel notu uygula.**\n" if has_note else "")
    return f"""\
# Kurallar
{note_rule}
## Havuz ve roller
- Her oyuncuya yalnızca **kendi havuzundaki** şampiyonlardan ver.
  Tercih sırası: {MAIN} > {PLAYABLE} > {STRETCH}. Öneri başına en fazla {stretch_limit} {STRETCH} pick olsun.
- 5'li comp'ta beş rol de dolu ve her oyuncu bir kez. Çekirdekte her rol en fazla bir kez;
  çekirdek dışındaki oyuncular kalan rolleri alabilmeli.
  **Meta rol kalıplarına takılma:** "Roller" sütunu ve rol matrisi oyuncunun şampiyonu gerçekte
  hangi rolde kaç maç oynadığını gösterir (ör. Kai'Sa Mid, Miss Fortune Support). Off-meta serbest.
- **"Oynamaz" (✕) rol o oyuncuya asla verilmez.** "Tercih eder" (✓) rolleri mümkünse kullan.
- **Esnek** oyunculara havuz dışı benzer bir şampiyon önerebilirsin; "havuz dışı" diye işaretle.

## Veriyi doğru oku
- Az maç gürültüdür: 5 maçtan az winrate'i kanıt sayma; maç sayısı ve mastery daha güvenilir.
  Maç sayısı 0 olan (yalnızca mastery'den gelen) şampiyonlarda "pas" riskini nota yaz.
- "Hasar%" takım hasarındaki pay, "CC sn" maç başı rakibi kontrol ettiği süre: kimin taşıdığını,
  kimin engage yaptığını bunlardan oku.
- **Birlikte oynadığınız maçlar** gerçek ortak maçlarımız. Çok maçlı, yüksek winrate'li ikililer
  kanıtlanmış silahlarımızdır: birkaç öneriyi bunların üstüne kur.
- **Rank** oyuncunun seviyesini gösterir: mekanik olarak zor şampiyonları ve hassas zamanlama
  isteyen combo'ları ekibin seviyesine göre ayarla.
- **Şampiyon notlarımız** grubun kendi gözlemidir; o şampiyonu değerlendirirken öncelik ver.

## Puanlama (her öneri için 1-5)
- **Wombo:** ulti/CC zincirinin gücü, kaç kişiyi yakaladığı, ıskalanınca ne kadar tolere ettiği.
- **Obje:** ejderha ve Baron'u ne kadar hızlı eritir (sürekli hasar, % can hasarı, obje hasarı/dk
  verisi) ve obje savaşında alanı ne kadar iyi tutar.
- **Keyif:** oynaması ne kadar eğlenceli: oyuncuların sevip çok oynadığı şampiyonlar, büyük
  takım savaşı anları, "highlight" potansiyeli.

## Mekanik doğruluk
- Her combo adımı gerçekten çalışmalı. Örnekler: Yasuo R yalnızca havaya kalkmış ya da savrulmuş
  hedeflere basılır (stun veya kök yetmez); kanallı ultiler (Miss Fortune, Katarina, Fiddlesticks,
  Malzahar...) CC ile kesilir, kilit süresine sığmalı; gecikmeli yetenekler (Cho'Gath Q, Lux R)
  önce kilitlenmiş hedefe atılmalı.
- Emin olmadığın etkileşimi kesin gibi yazma. Bilmediğin, yeni ya da rework almış şampiyonlarda
  **Şampiyon ulti özetleri**ni esas al; yine de emin değilsen o şampiyonu kullanma.

## Çeşitlilik
- Öneriler birbirinin kopyası olmasın: farklı engage kaynakları, farklı oyuncu grupları.
- Bir oyuncunun tek gerçek main'i her öneride kilit olmasın (banlanırsa hepsi çöker): en az
  üçte biri o şampiyon olmadan kurulsun.
{experimental}
# İstenen çıktı
**Kısa ve öz.** Genel LoL bilgisi anlatma, açıklama paragrafı yazma. Yalnızca şunlar:

Numaralar iki tablo boyunca devam etsin (ör. 5'liler 1-9, çekirdekler 10-18). Her tablo kendi
içinde üç puanın toplamına göre en iyiden başlasın.

1. **5'li comp'lar** (listenin yaklaşık yarısı):
   | # | Tip | Top | Jungle | Mid | ADC | Support | Sesli komut | Wombo | Obje | Keyif | Not |
   - Rol hücreleri: "Oyuncu Şampiyon" (ör. "civaHg Amumu")
2. **Wombo çekirdekleri** (3-4 kişi, kalan yarı):
   | # | Tip | Çekirdek | Sesli komut | Serbest slotlar | Wombo | Obje | Keyif | Not |
   - Çekirdek: "Oyuncu Şampiyon (Rol)" + ... (ör. "civaHg Amumu (Jg) + Ali MF (Sup) + Lao Jhin (ADC)")
   - Serbest slotlar: çekirdek dışındaki roller; çekirdeği belirgin güçlendiren biri varsa
     "isteğe bağlı: Oyuncu Şampiyon"
   İki tabloda da:
   - Tip: Sağlam / Deneysel
   - Sesli komut: ulti sırası tek satır (ör. "Amumu R → Jhin W → MF R")
   - Not: en fazla ~10 kelime, veriden kanıt ya da risk (ör. "ikili 14 maç %71", "Brand pas riski")
3. **Öne çıkanlar** (en fazla 3 madde, tek cümle): en güçlü wombo, en hızlı obje ve en keyifli
   öneri; bu akşam hangisiyle başlanmalı.
4. **Ortak banlar** (tek satır, en fazla 3 şampiyon): önerilerin çoğunu bozanlar.
5. **Belirsizlikler**: yalnızca varsa, tek satır.

Bir önerinin numarasını yazarsak o zaman ayrıntı ver: tam kadro, combo sırası, ilk 15 dakika,
draft sırası, yedek pickler. Draft sırasında rakip pickleri yazarsak öneriyi onlara göre daralt.

**Göndermeden önce kontrol et:** toplam en az {MIN_CORES} öneri var mı, yaklaşık yarısı 5'li
comp mu, kalanı 3-4 kişilik mi, 5'lilerde beş rol de dolu mu, hiçbir öneride "Oynamaz" rol ya da
havuz dışı (işaretsiz) şampiyon yok mu?

_Brief formatı v{__version__}_
"""


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
    if "obj_dpm" in style:
        parts.append(f"{style['obj_dpm']:.0f} obje hasarı/dk")
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
    if pool.ranks:
        lines.append("Rank: " + " · ".join(f"{q} {format_rank(e)}" for q, e in pool.ranks.items()))
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


def notes_section(notes, names):
    """Grubun şampiyonlar hakkındaki kendi gözlemleri (yalnızca havuzlarda geçenler)."""
    if not notes:
        return ""
    lines = ["## Şampiyon notlarımız (grubun kendi gözlemi)", ""]
    for ddid, note in sorted(notes.items(), key=lambda kv: names.get(_norm(kv[0]), kv[0]).lower()):
        lines.append(f"- **{names.get(_norm(ddid), ddid)}:** {note}")
    return "\n".join(lines)


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


def build_prompt(pools, names, shared=None, profile_names=None, mode="karma", kits=None, patch=None,
                 notes=None, special_note=None):
    """shared: synergy.shared_matches çıktısı; profile_names: profil id -> ad; mode: MODES anahtarı;
    kits: ddragon_id -> ulti özeti; patch: Data Dragon sürümü; notes: ddragon_id -> grubun notu."""
    parts = [task_intro(mode, patch, special_note), "## Oyuncular", "", LEGEND, ""]
    parts += [player_section(p, names) + "\n" for p in pools]
    parts.append(role_matrix_section(pools, names) + "\n")
    if shared is not None:
        parts.append(synergy_section(shared, profile_names, names) + "\n")
    note_text = notes_section(notes, names)
    if note_text:
        parts.append(note_text + "\n")
    kit_text = kits_section(kits, names, patch)
    if kit_text:
        parts.append(kit_text + "\n")
    parts.append(rules_and_format(mode, has_note=bool((special_note or "").strip())))
    return "\n".join(parts)
