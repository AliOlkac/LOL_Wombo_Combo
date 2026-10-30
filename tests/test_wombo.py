import unittest

from wombo.pool import MAIN, PLAYABLE, STRETCH, ChampStat, build_pool, comfort_score
from wombo.prompt import build_prompt
from wombo.riot import RateLimiter
from wombo.store import Store

NOW = 1_800_000_000_000
DAY = 86_400_000


def match(champ, win, pos="MIDDLE", queue=420, k=5, d=3, a=7, days_ago=1):
    return {"champion": champ, "win": int(win), "position": pos, "queue_id": queue,
            "kills": k, "deaths": d, "assists": a, "game_creation": NOW - days_ago * DAY}


class ComfortScoreTest(unittest.TestCase):
    def test_experienced_main_scores_high(self):
        s = ChampStat("Ahri", games=30, wins=17, kills=150, deaths=90, assists=200,
                      last_played=NOW - DAY, mastery_points=300_000)
        self.assertGreater(comfort_score(s, NOW), 80)

    def test_single_lost_game_is_not_playable(self):
        s = ChampStat("Yasuo", games=1, wins=0, kills=1, deaths=8, assists=2,
                      last_played=NOW - DAY, mastery_points=2_000)
        self.assertLess(comfort_score(s, NOW), 40)


class BuildPoolTest(unittest.TestCase):
    def test_tiers_roles_and_queue_filter(self):
        matches = [match("Ahri", i % 3 != 0) for i in range(20)]
        matches += [match("Orianna", True), match("Orianna", False), match("Orianna", True)]
        matches += [match("Jinx", True, queue=450) for _ in range(10)]  # ARAM sayılmaz
        mastery = [{"champion": "Malphite", "points": 150_000, "level": 7}]
        extras = [{"champion": "MonkeyKing", "tier": STRETCH, "note": "eski main"}]
        pool = build_pool(matches, mastery, extras, "ali", "Ali#TR1", now_ms=NOW)

        tiers = {c.champion: c.tier for c in pool.champions}
        self.assertEqual(tiers["Ahri"], MAIN)
        self.assertEqual(tiers["Malphite"], PLAYABLE)       # sadece mastery, yüksek
        self.assertEqual(tiers["MonkeyKing"], STRETCH)      # elle eklendi
        self.assertNotIn("Jinx", tiers)
        self.assertEqual(pool.sr_games, 23)
        self.assertEqual(pool.role_share, {"MIDDLE": 1.0})
        self.assertEqual(pool.champions[0].champion, "Ahri")

    def test_match_and_mastery_names_merge_case_insensitively(self):
        pool = build_pool([match("FiddleSticks", True)],
                          [{"champion": "Fiddlesticks", "points": 50_000, "level": 5}],
                          [], now_ms=NOW)
        self.assertEqual(len(pool.champions), 1)
        self.assertEqual(pool.champions[0].mastery_points, 50_000)


class StoreAndPromptTest(unittest.TestCase):
    def test_prompt_contains_players_and_display_names(self):
        store = Store(":memory:")
        store.set_champions([(62, "MonkeyKing", "Wukong"), (103, "Ahri", "Ahri")])
        self.assertEqual(store.find_champion("wukong"), "MonkeyKing")
        self.assertEqual(store.find_champion("Monkey King"), "MonkeyKing")

        pools = []
        for i in range(5):
            puuid = f"p{i}"
            store.add_player(puuid, f"Oyuncu{i}#TR1", f"oyuncu{i}")
            for n in range(10):
                store.add_match(f"m{i}-{n}", puuid, "Ahri", "MIDDLE", 5, 2, 8, True, NOW, 420)
            store.set_extra(puuid, "MonkeyKing", PLAYABLE, None)
            p = store.player(f"OYUNCU{i}")
            pools.append(build_pool(store.matches(puuid), store.mastery(puuid),
                                    store.extras(puuid), p["nickname"], p["riot_id"], now_ms=NOW))
        text = build_prompt(pools, store.champion_names())
        self.assertIn("### oyuncu4 (Oyuncu4#TR1)", text)
        self.assertIn("| Wukong | Oynayabilir |", text)
        self.assertIn("Mid %100", text)


class RateLimiterTest(unittest.TestCase):
    def test_waits_when_window_full(self):
        t = [0.0]
        slept = []

        def sleep(d):
            slept.append(d)
            t[0] += d

        limiter = RateLimiter(limits=((2, 1.0),), clock=lambda: t[0], sleep=sleep)
        for _ in range(3):
            limiter.wait()
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(t[0], 1.0)


if __name__ == "__main__":
    unittest.main()
