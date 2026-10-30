"""Oyuncu başına şampiyon havuzu ve rahatlık skoru."""
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field

from .store import _norm

# Sihirdar Vadisi kuyrukları: normal draft, solo/duo, blind, flex, quickplay, clash
SR_QUEUES = {400, 420, 430, 440, 490, 700}
ARAM_QUEUES = {450}


class StatAverager:
    """Maç başı istatistiklerin ortalaması (her alan kendi örnek sayısıyla)."""

    def __init__(self):
        self.sums = {}
        self.counts = {}

    def add(self, stats):
        for key, value in (stats or {}).items():
            self.sums[key] = self.sums.get(key, 0) + value
            self.counts[key] = self.counts.get(key, 0) + 1

    def averages(self):
        return {k: self.sums[k] / self.counts[k] for k in self.sums}


TIERS = ("IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND",
         "MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = ("IV", "III", "II", "I")
QUEUE_NAMES = {"RANKED_SOLO_5x5": "Solo", "RANKED_FLEX_SR": "Flex"}


def best_ranks(entries):
    """Birden fazla hesabın dereceli kayıtlarından kuyruk başına en yükseği: {"Solo": kayıt, ...}."""
    def strength(e):
        tier = TIERS.index(e["tier"]) if e.get("tier") in TIERS else -1
        division = DIVISIONS.index(e["rank"]) if e.get("rank") in DIVISIONS else 0
        return tier, division, e.get("leaguePoints", 0)

    best = {}
    for e in entries:
        queue = QUEUE_NAMES.get(e.get("queueType"))
        if queue and (queue not in best or strength(e) > strength(best[queue])):
            best[queue] = e
    return {q: best[q] for q in ("Solo", "Flex") if q in best}


def format_rank(entry, detail=True):
    """{"tier": "GOLD", "rank": "II", ...} -> "Gold II 45 LP (%52, 120 maç)"."""
    tier = entry["tier"].title()
    text = tier if entry["tier"] in ("MASTER", "GRANDMASTER", "CHALLENGER") else f"{tier} {entry['rank']}"
    if not detail:
        return text
    games = entry.get("wins", 0) + entry.get("losses", 0)
    wr = f"%{round(100 * entry['wins'] / games)}, " if games else ""
    return f"{text} {entry.get('leaguePoints', 0)} LP ({wr}{games} maç)"


def _row_stats(row):
    """Maç satırındaki JSON istatistikler (sütun yoksa ya da boşsa None)."""
    try:
        raw = row["stats"]
    except (KeyError, IndexError):
        return None
    if isinstance(raw, dict):
        return raw
    return json.loads(raw) if raw else None

MAIN, PLAYABLE, STRETCH = "Main", "Oynayabilir", "Esneme"
TIER_ORDER = {MAIN: 0, PLAYABLE: 1, STRETCH: 2}
TIER_ALIASES = {"main": MAIN, "oynayabilir": PLAYABLE, "esneme": STRETCH}

POSITION_NAMES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ROLE_ALIASES = {
    "top": "TOP", "ust": "TOP",
    "jungle": "JUNGLE", "jg": "JUNGLE", "jungler": "JUNGLE", "orman": "JUNGLE",
    "mid": "MIDDLE", "middle": "MIDDLE", "orta": "MIDDLE",
    "adc": "BOTTOM", "bot": "BOTTOM", "bottom": "BOTTOM",
    "support": "UTILITY", "sup": "UTILITY", "supp": "UTILITY", "destek": "UTILITY", "utility": "UTILITY",
}


def parse_roles(text):
    """'jg, mid' -> ['JUNGLE', 'MIDDLE']. Bilinmeyen rol için ValueError."""
    roles = []
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        role = ROLE_ALIASES.get(part.lower()) or (part.upper() if part.upper() in POSITION_NAMES else None)
        if role is None:
            raise ValueError(f"Bilinmeyen rol: {part}")
        if role not in roles:
            roles.append(role)
    return roles

DAY_MS = 86_400_000
FLEX_BREADTH = 8  # bu kadar Main+Oynayabilir şampiyonu olan oyuncu "esnek" sayılır


@dataclass
class ChampStat:
    champion: str
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    last_played: int = 0
    positions: Counter = field(default_factory=Counter)
    mastery_points: int = 0
    mastery_level: int = 0
    extra_tier: str = None
    note: str = None
    score: float = 0.0
    tier: str = None
    style: StatAverager = field(default_factory=StatAverager)

    @property
    def winrate(self):
        return self.wins / self.games if self.games else 0.0

    @property
    def kda(self):
        return (self.kills + self.assists) / max(self.deaths, 1)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def comfort_score(stat, now_ms):
    """0-100 arası rahatlık skoru.

    Deneyim (maç sayısı) ağırlıklıdır; winrate ve KDA, az maçta yanıltmasın diye
    deneyimle çarpılır ve winrate %50'ye doğru düzeltilir.
    """
    experience = 1 - math.exp(-stat.games / 6)
    smoothed_wr = (stat.wins + 3) / (stat.games + 6)
    performance = 0.6 * _clamp((smoothed_wr - 0.35) / 0.3) + 0.4 * _clamp(stat.kda / 5)
    days = (now_ms - stat.last_played) / DAY_MS if stat.last_played else 365
    recency = math.exp(-days / 60)
    mastery = _clamp(math.log10(stat.mastery_points + 1) / 6)
    return 100 * (0.40 * experience
                  + 0.20 * performance * experience
                  + 0.15 * recency * experience
                  + 0.25 * mastery)


def tier_for(stat):
    if stat.extra_tier:
        return stat.extra_tier
    if stat.games == 0:
        # Son maçlarda yok ama ustalığı yüksek: eski main olabilir
        if stat.mastery_points >= 100_000:
            return PLAYABLE
        if stat.mastery_points >= 30_000:
            return STRETCH
        return None
    if stat.score >= 60:
        return MAIN
    if stat.score >= 40:
        return PLAYABLE
    if stat.score >= 20:
        return STRETCH
    return None


@dataclass
class PlayerPool:
    name: str
    riot_ids: list
    champions: list
    role_share: dict
    sr_games: int
    preferred_roles: list = field(default_factory=list)
    avoided_roles: list = field(default_factory=list)
    note: str = ""
    style: dict = field(default_factory=dict)   # SR maç başı ortalamalar (hasar payı, CC süresi...)
    aram: list = field(default_factory=list)    # [(şampiyon, maç)] — havuza sayılmaz
    ranks: dict = field(default_factory=dict)  # {"Solo": kayıt, "Flex": kayıt} (en yüksek hesap)
    role_counts: dict = field(default_factory=dict)  # rol -> SR maç sayısı
    role_champs: dict = field(default_factory=dict)  # rol -> [(şampiyon, maç)] en çok oynananlar

    @property
    def breadth(self):
        return sum(1 for c in self.champions if c.tier in (MAIN, PLAYABLE))

    @property
    def flexible(self):
        return self.breadth >= FLEX_BREADTH


def build_pool(match_rows, mastery_rows, extra_rows, name="", riot_ids=(), preferred_roles=(),
               avoided_roles=(), note="", now_ms=None, rank_entries=()):
    """Bir profilin (tüm hesaplarının) verisinden şampiyon havuzunu çıkarır."""
    now_ms = now_ms or int(time.time() * 1000)
    stats = {}

    def get(champion):
        key = _norm(champion)
        if key not in stats:
            stats[key] = ChampStat(champion=champion)
        return stats[key]

    roles = Counter()
    overall = StatAverager()
    aram = Counter()
    for m in match_rows:
        if m["queue_id"] in ARAM_QUEUES:
            aram[m["champion"]] += 1
        if m["queue_id"] not in SR_QUEUES:
            continue
        s = get(m["champion"])
        match_stats = _row_stats(m)
        s.style.add(match_stats)
        overall.add(match_stats)
        s.games += 1
        s.wins += m["win"]
        s.kills += m["kills"]
        s.deaths += m["deaths"]
        s.assists += m["assists"]
        s.last_played = max(s.last_played, m["game_creation"])
        if m["position"]:
            s.positions[m["position"]] += 1
            roles[m["position"]] += 1

    for r in mastery_rows:  # birden fazla hesap varsa puanlar toplanır
        s = get(r["champion"])
        s.mastery_points += r["points"]
        s.mastery_level = max(s.mastery_level, r["level"])

    for r in extra_rows:
        s = get(r["champion"])
        s.extra_tier = r["tier"]
        s.note = r["note"]

    champions = []
    for s in stats.values():
        s.score = comfort_score(s, now_ms)
        s.tier = tier_for(s)
        if s.tier:
            champions.append(s)
    champions.sort(key=lambda c: (TIER_ORDER[c.tier], -c.score))

    total = sum(roles.values())
    role_share = {pos: n / total for pos, n in roles.most_common()} if total else {}
    sr_games = sum(1 for m in match_rows if m["queue_id"] in SR_QUEUES)
    role_champs = {}
    for role in roles:
        played = [(s.champion, s.positions[role]) for s in stats.values() if s.positions[role]]
        role_champs[role] = sorted(played, key=lambda cn: -cn[1])[:3]
    return PlayerPool(name, list(riot_ids), champions, role_share, sr_games,
                      list(preferred_roles), list(avoided_roles), note or "",
                      style=overall.averages(), aram=aram.most_common(),
                      ranks=best_ranks(rank_entries), role_counts=dict(roles), role_champs=role_champs)
