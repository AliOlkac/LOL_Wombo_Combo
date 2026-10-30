"""Takım havuzlarından Claude'a verilecek wombo combo brief'ini üretir."""
from .pool import MAIN, PLAYABLE, POSITION_NAMES, STRETCH
from .store import _norm

FLEX_BREADTH = 8      # bu kadar Main+Oynayabilir şampiyonu olan oyuncu "esnek" sayılır
MAX_CHAMPS = 25       # oyuncu başına brief'e girecek en fazla şampiyon

INSTRUCTIONS = f"""\
Sen bir League of Legends koçusun. Aşağıda 5 kişilik premade takımımızın gerçek
maç geçmişinden çıkarılmış şampiyon havuzları var. Bu 5 kişiyle oynanabilecek
**wombo combo** takımları kur: takım savaşında üst üste binen AoE engage,
knockup/çekme ve AoE hasar ultilerinden oluşan diziliş.

## Kurallar
- Her oyuncuya yalnızca **kendi havuzundaki** şampiyonlardan ver.
  Tercih sırası: {MAIN} > {PLAYABLE} > {STRETCH}. Comp başına en fazla bir {STRETCH} pick olsun.
- Rolleri oyuncuların rol dağılımına göre dağıt; her rol (Top, Jungle, Mid, ADC, Support) bir kez.
- **Esnek** işaretli oyunculara, havuzlarında olmasa da oynadıklarına çok benzeyen bir
  şampiyon önerebilirsin. Bunu mutlaka "havuz dışı öneri" diye işaretle ve neden benzediğini söyle.
- Gerçekçi ol: combo'nun parçalarından biri oyuncunun zayıf şampiyonuysa bunu belirt.

## İstenen çıktı
3 farklı wombo comp. Her biri için:
1. Oyuncu → rol → şampiyon (havuz seviyesi)
2. Combo nasıl işliyor: kim açar, ultiler hangi sırayla basılır
3. Kazanma planı ve güç zamanı (erken / orta / geç oyun)
4. Zayıf yönler ve bu comp'u bozan şampiyonlar (ban önerisi)
5. Kilit şampiyon banlanırsa her oyuncu için yedek pick

En sonda draftta hangi sırayla pick yapılması gerektiğini (combo'yu erken belli etmeden) kısaca öner.
"""


def _pct(x):
    return f"%{round(x * 100)}"


def player_section(pool, names):
    flexible = pool.breadth >= FLEX_BREADTH
    title = f"### {pool.nickname} ({pool.riot_id})" + (" — Esnek" if flexible else "")
    roles = ", ".join(f"{POSITION_NAMES.get(p, p)} {_pct(s)}" for p, s in pool.role_share.items())
    lines = [title, f"Son {pool.sr_games} Sihirdar Vadisi maçı. Rol dağılımı: {roles or 'veri yok'}", ""]
    if not pool.champions:
        lines.append("_Havuz boş: maç verisi yok._")
        return "\n".join(lines)
    lines += ["| Şampiyon | Seviye | Maç | WR | KDA | Roller | Mastery | Not |",
              "|---|---|---|---|---|---|---|---|"]
    for c in pool.champions[:MAX_CHAMPS]:
        name = names.get(_norm(c.champion), c.champion)
        pos = ", ".join(POSITION_NAMES.get(p, p) for p, _ in c.positions.most_common(2)) or "-"
        if c.games:
            wr, kda = _pct(c.winrate), f"{c.kda:.1f}"
        else:
            wr = kda = "-"
        mastery = f"{c.mastery_points // 1000}k" if c.mastery_points else "-"
        note = c.note or ("elle eklendi" if c.extra_tier else "")
        lines.append(f"| {name} | {c.tier} | {c.games} | {wr} | {kda} | {pos} | {mastery} | {note} |")
    return "\n".join(lines)


def build_prompt(pools, names):
    parts = [INSTRUCTIONS, "## Oyuncular", ""]
    parts += [player_section(p, names) + "\n" for p in pools]
    return "\n".join(parts)
