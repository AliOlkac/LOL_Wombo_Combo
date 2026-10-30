"""Takım havuzlarından Claude'a verilecek wombo combo brief'ini üretir."""
from .pool import MAIN, PLAYABLE, POSITION_NAMES, STRETCH
from .store import _norm
from .synergy import duo_stats, pair_totals

MAX_CHAMPS = 25       # oyuncu başına brief'e girecek en fazla şampiyon
MAX_DUOS = 15         # brief'e girecek en fazla şampiyon ikilisi
MAX_GROUP_GAMES = 8   # en az 3 kişiyle oynanmış son maçlardan kaç tanesi listelensin

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


def instructions(mode="karma"):
    if mode not in MODES:
        raise ValueError(f"Bilinmeyen mod: {mode}. Seçenekler: {', '.join(MODES)}")
    stretch_limit = "iki" if mode == "deneysel" else "bir"
    if mode == "klasik":
        task = "3 farklı sağlam wombo comp kur."
    elif mode == "karma":
        task = "3 farklı wombo comp kur: ilk ikisi sağlam, **üçüncüsü deneysel** (aşağıya bak)."
    else:
        task = "3 farklı **deneysel** wombo comp kur (aşağıya bak)."
    text = f"""\
Sen bir League of Legends koçusun. Aşağıda 5 kişilik premade takımımızın gerçek
maç geçmişinden çıkarılmış şampiyon havuzları var. Bu 5 kişiyle oynanabilecek
**wombo combo** takımları kur: takım savaşında üst üste binen AoE engage,
knockup/çekme ve AoE hasar ultilerinden oluşan diziliş. {task}

## Kurallar
- Her oyuncuya yalnızca **kendi havuzundaki** şampiyonlardan ver.
  Tercih sırası: {MAIN} > {PLAYABLE} > {STRETCH}. Comp başına en fazla {stretch_limit} {STRETCH} pick olsun.
- Her rol (Top, Jungle, Mid, ADC, Support) bir kez. **Meta rol kalıplarına takılma:** tablodaki
  "Roller" sütunu oyuncunun o şampiyonu gerçekte hangi rolde kaç maç oynadığını gösterir
  (ör. Kai'Sa Mid, Miss Fortune Support). Şampiyonu oyuncunun oynadığı rolde değerlendir;
  off-meta seçimler serbest, iyi çalışıyorsa tercih sebebidir. **"Oynamaz" yazan rol o oyuncuya
  asla verilmez.**
- **Esnek** işaretli oyunculara, havuzlarında olmasa da oynadıklarına çok benzeyen bir
  şampiyon önerebilirsin. Bunu mutlaka "havuz dışı öneri" diye işaretle ve neden benzediğini söyle.
- Gerçekçi ol: combo'nun parçalarından biri oyuncunun zayıf şampiyonuysa bunu belirt.
- En sondaki **Birlikte oynadığınız maçlar** bölümü gerçek ortak maçlarımızdan çıkarıldı.
  Çok maçlı ve yüksek winrate'li ikililer kanıtlanmış silahlarımızdır: en az bir comp'u bunlardan
  birinin etrafına kur ve onu daha da güçlendirecek (engage, AoE, koruma) parçaları ekle.
  2-3 maçlık ikililere fazla güvenme.
- "Hasar%" takım hasarındaki payı, "CC sn" maç başı rakibi kontrol ettiği süreyi gösterir;
  kimin taşıdığını, kimin engage yaptığını bunlardan da oku.

## İstenen çıktı
Her comp için:
1. Oyuncu → rol → şampiyon (havuz seviyesi)
2. Combo nasıl işliyor: kim açar, ultiler hangi sırayla basılır
3. Kazanma planı ve güç zamanı (erken / orta / geç oyun)
4. Zayıf yönler ve bu comp'u bozan şampiyonlar (ban önerisi)
5. Kilit şampiyon banlanırsa her oyuncu için yedek pick

En sonda draftta hangi sırayla pick yapılması gerektiğini (combo'yu erken belli etmeden) kısaca öner.
"""
    if mode != "klasik":
        text += EXPERIMENTAL
    return text


def _champ_role(champ_names, champion, position):
    name = champ_names.get(_norm(champion or ""), champion or "?")
    return f"{name} ({POSITION_NAMES.get(position, position)})" if position else name


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


def _pct(x):
    return f"%{round(x * 100)}"


def _roles(roles):
    return ", ".join(POSITION_NAMES.get(r, r) for r in roles)


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


def build_prompt(pools, names, shared=None, profile_names=None, mode="karma"):
    """shared: synergy.shared_matches çıktısı; profile_names: profil id -> ad; mode: MODES anahtarı."""
    parts = [instructions(mode), "## Oyuncular", ""]
    parts += [player_section(p, names) + "\n" for p in pools]
    if shared is not None:
        parts.append(synergy_section(shared, profile_names, names))
    return "\n".join(parts)
