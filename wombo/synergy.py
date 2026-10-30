"""Birlikte oynanan maçlardan kanıtlanmış ikililer ve ortak kadrolar.

Ör. "Ali Miss Fortune (Support) + ŁĄÎŒÏ Jhin (ADC): 14 maç, %71" gibi, havuz
tablosundan tahmin edilemeyen ama gerçekte işe yarayan kombinasyonları bulur.
"""
from collections import defaultdict
from dataclasses import dataclass

from .pool import SR_QUEUES


@dataclass
class SharedMatch:
    match_id: str
    game_creation: int
    win: bool
    players: dict  # profil id -> (şampiyon, rol)


def shared_matches(rows, min_players=2):
    """Seçili profillerden en az min_players kişinin aynı takımda olduğu SR maçları (yeniden eskiye)."""
    by_match = {}
    for r in rows:
        if r["queue_id"] not in SR_QUEUES:
            continue
        m = by_match.setdefault(r["match_id"],
                                SharedMatch(r["match_id"], r["game_creation"], bool(r["win"]), {}))
        m.players.setdefault(r["profile_id"], (r["champion"], r["position"]))
    found = [m for m in by_match.values() if len(m.players) >= min_players]
    return sorted(found, key=lambda m: -m.game_creation)


def _pairs(match):
    ids = sorted(match.players)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            yield a, b


def pair_totals(matches):
    """(profil_a, profil_b) -> [maç, galibiyet], şampiyondan bağımsız."""
    totals = defaultdict(lambda: [0, 0])
    for m in matches:
        for a, b in _pairs(m):
            totals[(a, b)][0] += 1
            totals[(a, b)][1] += m.win
    return sorted(totals.items(), key=lambda kv: -kv[1][0])


def duo_stats(matches, min_games=2):
    """((profil_a, şampiyon_a, rol_a, profil_b, şampiyon_b, rol_b), maç, galibiyet) listesi.

    Çok oynanan ve kazandıran ikililer üstte: önce maç sayısı, sonra winrate.
    """
    stats = defaultdict(lambda: [0, 0])
    for m in matches:
        for a, b in _pairs(m):
            key = (a, *m.players[a], b, *m.players[b])
            stats[key][0] += 1
            stats[key][1] += m.win
    rows = [(key, games, wins) for key, (games, wins) in stats.items() if games >= min_games]
    rows.sort(key=lambda r: (-r[1], -r[2] / r[1]))
    return rows
