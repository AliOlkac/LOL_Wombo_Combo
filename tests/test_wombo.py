import json
import os
import tempfile
import threading
import unittest
import urllib.request
from urllib.error import HTTPError

from wombo import service
from wombo.pool import MAIN, PLAYABLE, STRETCH, ChampStat, build_pool, comfort_score, parse_roles
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
        pool = build_pool(matches, mastery, extras, "Ali", ["Ali#TR1"], now_ms=NOW)

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

    def test_mastery_is_summed_across_accounts(self):
        pool = build_pool([], [{"champion": "Malphite", "points": 60_000, "level": 6},
                               {"champion": "Malphite", "points": 50_000, "level": 7}], [], now_ms=NOW)
        self.assertEqual(pool.champions[0].mastery_points, 110_000)
        self.assertEqual(pool.champions[0].tier, PLAYABLE)

    def test_parse_roles(self):
        self.assertEqual(parse_roles("jg, Mid,sup,jungle"), ["JUNGLE", "MIDDLE", "UTILITY"])
        self.assertEqual(parse_roles(""), [])
        with self.assertRaises(ValueError):
            parse_roles("kale")


def seed_team(store):
    """5 profil; Ali'nin iki hesabı var."""
    store.set_champions("15.1.1", [(62, "MonkeyKing", "Wukong"), (103, "Ahri", "Ahri"),
                                   (54, "Malphite", "Malphite")])
    ids = []
    for i in range(5):
        pid = store.create_profile(f"Oyuncu{i}")
        ids.append(pid)
        store.add_account(f"p{i}", f"Oyuncu{i}#TR1", pid)
        for n in range(10):
            store.add_match(f"m{i}-{n}", f"p{i}", "Ahri", "MIDDLE", 5, 2, 8, True, NOW, 420)
    store.add_account("p0-smurf", "AliSmurf#TR1", ids[0])
    for n in range(6):
        store.add_match(f"s-{n}", "p0-smurf", "Malphite", "TOP", 3, 2, 9, True, NOW, 420)
    store.db.commit()
    return ids


class StoreAndPromptTest(unittest.TestCase):
    def test_profiles_accounts_and_prompt(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        ids = seed_team(store)
        self.assertEqual(store.find_champion("wukong"), "MonkeyKing")
        self.assertEqual(store.find_champion("Monkey King"), "MonkeyKing")
        store.update_profile(ids[0], preferred_roles="TOP", avoided_roles="UTILITY", note="tank sever")
        store.set_extra(ids[1], "MonkeyKing", PLAYABLE, None)

        pools = [service.load_pool(store, store.profile(i)) for i in ids]
        self.assertEqual(pools[0].sr_games, 16)  # iki hesabın maçları birleşir
        self.assertEqual(sorted(pools[0].riot_ids), ["AliSmurf#TR1", "Oyuncu0#TR1"])

        text = build_prompt(pools, store.champion_names())
        self.assertIn("### Oyuncu4 (Oyuncu4#TR1)", text)
        self.assertIn("| Wukong | Oynayabilir |", text)
        self.assertIn("Tercih ettiği roller: Top", text)
        self.assertIn("Oynamaz: Support", text)
        self.assertIn("Not: tank sever", text)

    def test_delete_profile_removes_its_data(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        ids = seed_team(store)
        store.delete_profile(ids[0])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM matches WHERE puuid LIKE 'p0%'").fetchone()[0], 0)
        self.assertEqual(len(store.accounts()), 4)

    def test_profile_lookup_by_name_is_case_insensitive(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.create_profile("Ayşe")
        self.assertEqual(store.profile("Ayşe")["name"], "Ayşe")
        self.assertEqual(store.profile(" aYşe ")["id"], 1)
        with self.assertRaises(KeyError):
            store.profile("yok")


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


class WebApiTest(unittest.TestCase):
    """Gerçek HTTP sunucusu üzerinden uçlar (Riot'a gitmeyen kısımlar)."""

    def setUp(self):
        from wombo.web import make_server
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "w.db")
        store = Store(self.db)
        self.ids = seed_team(store)
        store.close()
        self.server = make_server(self.db, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, content_type="application/json"):
        data = json.dumps(body or {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_index_and_state(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"WOMBO", r.read())
        code, state = self.call("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(len(state["profiles"]), 5)
        self.assertEqual(state["ddragon_version"], "15.1.1")
        ali = next(p for p in state["profiles"] if p["name"] == "Oyuncu0")
        self.assertEqual(len(ali["accounts"]), 2)
        self.assertEqual({c["id"] for c in ali["champions"]}, {"Ahri", "Malphite"})

    def test_profile_crud_roles_extras_and_brief(self):
        code, created = self.call("POST", "/api/profiles", {"name": "Yeni"})
        self.assertEqual(code, 200)
        code, err = self.call("POST", "/api/profiles", {"name": "yeni"})
        self.assertEqual(code, 400)

        pid = self.ids[2]
        code, _ = self.call("POST", f"/api/profiles/{pid}",
                            {"preferred_roles": ["JUNGLE"], "avoided_roles": ["UTILITY"], "note": "x"})
        self.assertEqual(code, 200)
        code, _ = self.call("POST", f"/api/profiles/{pid}/extras",
                            {"champion": "wukong", "tier": "main", "note": "eski main"})
        self.assertEqual(code, 200)
        code, detail = self.call("GET", f"/api/profiles/{pid}")
        self.assertEqual(detail["preferred_roles"], ["JUNGLE"])
        wk = next(c for c in detail["champions"] if c["id"] == "MonkeyKing")
        self.assertEqual((wk["tier"], wk["manual"], wk["name"]), (MAIN, True, "Wukong"))

        code, _ = self.call("DELETE", f"/api/profiles/{pid}/extras/MonkeyKing")
        self.assertEqual(code, 200)

        code, err = self.call("POST", "/api/brief", {"profile_ids": self.ids[:4]})
        self.assertEqual(code, 400)
        code, brief = self.call("POST", "/api/brief", {"profile_ids": self.ids})
        self.assertEqual(code, 200)
        self.assertIn("Oynamaz: Support", brief["text"])

        code, _ = self.call("DELETE", f"/api/profiles/{created['id']}")
        self.assertEqual(code, 200)
        code, _ = self.call("GET", f"/api/profiles/{created['id']}")
        self.assertEqual(code, 404)

    def test_non_json_post_is_rejected(self):
        code, _ = self.call("POST", "/api/profiles", {"name": "X"}, content_type="text/plain")
        self.assertEqual(code, 415)


if __name__ == "__main__":
    unittest.main()
