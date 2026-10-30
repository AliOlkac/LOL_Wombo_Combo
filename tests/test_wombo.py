import json
import os
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock
from urllib.error import HTTPError

from wombo import service
from wombo.pool import MAIN, PLAYABLE, STRETCH, ChampStat, build_pool, comfort_score, parse_roles
from wombo.prompt import build_prompt
from wombo.riot import RateLimiter, RiotAuthError, RiotClient, RiotError
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


def participant(puuid, name, team, win, champ="Ahri", pos="MIDDLE"):
    game_name, tag = name.split("#")
    return {"puuid": puuid, "riotIdGameName": game_name, "riotIdTagline": tag, "teamId": team,
            "win": win, "championName": champ, "teamPosition": pos,
            "kills": 5, "deaths": 2, "assists": 7}


class FakeClient:
    """Riot API yerine hazır maçlar döner. matches: {match_id: [katılımcılar]}."""

    def __init__(self, matches):
        self.matches = matches
        self.mastery_platforms = []

    def match_ids(self, puuid, start=0, count=100):
        ids = [m for m, parts in self.matches.items() if any(p["puuid"] == puuid for p in parts)]
        return ids[start:start + count]

    def match(self, match_id):
        return {"info": {"gameCreation": NOW, "queueId": 420, "participants": self.matches[match_id]}}

    def masteries(self, puuid, platform=None):
        self.mastery_platforms.append(platform)
        return [{"championId": 103, "championPoints": 50_000, "championLevel": 5}]


def friend_matches():
    """Ben 5 maç: Ayşe 5'inde, Can 3'ünde yanımda; Rakip 4 kez karşı takımda."""
    matches = {}
    for i in range(5):
        parts = [participant("me", "Oqila#4310", 100, i != 0),
                 participant("ayse", "Ayşe#TR1", 100, i != 0, "Yasuo")]
        if i < 3:
            parts.append(participant("can", "Can#TR1", 100, i != 0, "MissFortune", "BOTTOM"))
        parts.append(participant(f"random{i}", f"Rastgele{i}#TR1", 100, i != 0))
        if i < 4:
            parts.append(participant("rakip", "Rakip#TR1", 200, i == 0))
        matches[f"TR1_{i}"] = parts
    return matches


class FriendDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.set_champions("15.1.1", [(103, "Ahri", "Ahri")])
        self.me = self.store.create_profile("Ben")
        self.store.add_account("me", "Oqila#4310", self.me)
        self.client = FakeClient(friend_matches())
        service.update_account(self.client, self.store, self.store.accounts(self.me)[0])

    def test_teammates_suggested_enemies_and_randoms_ignored(self):
        friends = self.store.suggest_friends(3)
        self.assertEqual([(f["riot_id"], f["games"], f["wins"]) for f in friends],
                         [("Ayşe#TR1", 5, 4), ("Can#TR1", 3, 2)])
        self.assertEqual(friends[0]["champions"], ["Yasuo"])
        self.assertEqual(self.client.mastery_platforms, ["tr1"])  # sunucu maç ID'sinden
        self.assertEqual(self.store.profile_mastery(self.me)[0]["champion"], "Ahri")

    def test_linking_removes_suggestion_and_shared_matches_count_once(self):
        pid, riot_id = service.link_suggested_account(self.store, "ayse", profile_name="Ayşe")
        self.assertEqual(riot_id, "Ayşe#TR1")
        # Ayşe'nin kendi geçmişi de aynı 5 maçı içerir; Can yine 3 maç sayılmalı (6 değil)
        service.update_account(self.client, self.store, self.store.accounts(pid)[0])
        friends = self.store.suggest_friends(3)
        self.assertEqual([(f["riot_id"], f["games"]) for f in friends], [("Can#TR1", 3)])

    def test_link_as_smurf_to_existing_profile(self):
        service.link_suggested_account(self.store, "can", profile_id=self.me)
        self.assertEqual(len(self.store.accounts(self.me)), 2)
        with self.assertRaises(KeyError):
            service.link_suggested_account(self.store, "yok")


class AccountClient:
    """Sadece hesap doğrulama ve anahtar testi yapan sahte istemci."""

    def __init__(self, accounts=None, status_error=None):
        self.accounts = accounts or {"oqila#4310": ("me", "Oqila", "4310")}
        self.status_error = status_error

    def account_by_riot_id(self, game_name, tag):
        found = self.accounts.get(f"{game_name}#{tag}".lower())
        return {"puuid": found[0], "gameName": found[1], "tagLine": found[2]} if found else None

    def platform_status(self):
        if self.status_error:
            raise self.status_error
        return {"id": "TR1"}


class CreateProfileTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.client = AccountClient()

    def create(self, name, riot_id=None):
        return service.create_profile(self.store, lambda: self.client, name, riot_id)

    def test_verified_account_creates_profile_with_correct_casing(self):
        pid, riot_id = self.create("Oqila", "oqila#4310")
        self.assertEqual(riot_id, "Oqila#4310")
        self.assertEqual(self.store.accounts(pid)[0]["puuid"], "me")

    def test_unknown_riot_id_creates_nothing(self):
        with self.assertRaises(RiotError):
            self.create("Oqila", "Yanlis#0000")
        self.assertEqual(self.store.profiles(), [])

    def test_riot_id_typed_into_name_field_is_split(self):
        pid, riot_id = self.create("Oqila#4310")
        self.assertEqual((self.store.profile(pid)["name"], riot_id), ("Oqila", "Oqila#4310"))

    def test_empty_profile_from_failed_attempt_is_reused(self):
        old = self.store.create_profile("Oqila")
        pid, _ = self.create("oqila", "Oqila#4310")
        self.assertEqual(pid, old)
        self.assertEqual(len(self.store.profiles()), 1)

    def test_duplicate_account_or_name_is_rejected(self):
        self.create("Oqila", "Oqila#4310")
        with self.assertRaisesRegex(ValueError, "zaten Oqila profiline bağlı"):
            self.create("Baska", "Oqila#4310")
        self.client.accounts["smurf#tr1"] = ("smurf", "Smurf", "TR1")
        with self.assertRaisesRegex(ValueError, "zaten var"):
            self.create("Oqila", "Smurf#TR1")
        self.assertEqual(len(self.store.profiles()), 1)

    def test_describe_error_messages(self):
        import socket
        import ssl
        from urllib.error import URLError
        ssl_err = URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
        self.assertIn("SSL sertifika", service.describe_error(ssl_err))
        self.assertIn("internet", service.describe_error(URLError(socket.gaierror(11001, "getaddrinfo failed"))))
        self.assertIn("süresi dolmuş", service.describe_error(RiotAuthError("API anahtarı geçersiz ya da süresi dolmuş")))


FAKE_KEY = "RGAPI-00000000-1111-2222-3333-444444444444"


class KeyHandlingTest(unittest.TestCase):
    def test_clean_key_strips_noise(self):
        for raw in [FAKE_KEY, f"  {FAKE_KEY}\n", f'apikey:"{FAKE_KEY}"', f"﻿{FAKE_KEY}",
                    FAKE_KEY[:20] + "​" + FAKE_KEY[20:]]:
            self.assertEqual(service.clean_key(raw), FAKE_KEY)
        self.assertIsNone(service.clean_key("merhaba"))

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data", "riot_key.txt")
            with self.assertRaisesRegex(ValueError, "eksik"):
                service.save_api_key("RGAPI-1234", path)
            service.save_api_key(f"apikey: {FAKE_KEY}", path)
            self.assertEqual(service.key_source(path), (FAKE_KEY, path))

    def test_client_sends_user_agent_and_reports_riot_message(self):
        import io
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured.update(req.header_items())
            raise HTTPError(req.full_url, 403, "Forbidden", {},
                            io.BytesIO(b'{"status":{"message":"Forbidden","status_code":403}}'))

        client = RiotClient(api_key=FAKE_KEY, limiter=RateLimiter(limits=((1000, 1.0),)))
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaisesRegex(RiotAuthError, "HTTP 403: Forbidden"):
                client.platform_status()
        self.assertEqual(captured["X-riot-token"], FAKE_KEY)
        self.assertIn("LOL-Wombo-Combo", captured["User-agent"])

    def diagnose(self, responses):
        store = Store(":memory:")
        self.addCleanup(store.close)
        answers = iter(responses)
        with mock.patch.object(service, "_probe", lambda url, key=None: next(answers)), \
                mock.patch.object(service, "key_source", lambda key_file=None: (FAKE_KEY, "test")):
            return service.diagnose(store, "Oqila#4310")

    def test_diagnose_interpretations(self):
        text = self.diagnose([(401, "Unauthorized"), (403, "Forbidden"), (403, "Forbidden")])
        self.assertIn("reddediyor (403)", text)
        self.assertNotIn(FAKE_KEY, text)  # rapor paylaşılabilir: tam anahtar yok
        self.assertIn("RGAPI-0000…4444", text)
        self.assertIn("Anahtar çalışıyor.", self.diagnose([(401, ""), (200, "OK"), (200, "OK")]))
        self.assertIn("tanımıyor", self.diagnose([(401, ""), (401, "Unknown apikey"), (401, "")]))
        self.assertIn("ağda", self.diagnose([(None, "Ağ hatası"), (None, ""), (None, "")]))
        self.assertIn("bulunamadı", self.diagnose([(401, ""), (200, "OK"), (404, "")]))


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

    def test_friend_suggestions_endpoint(self):
        store = Store(self.db)
        me = store.create_profile("Ben")
        store.add_account("me", "Oqila#4310", me)
        service.update_account(FakeClient(friend_matches()), store, store.accounts(me)[0])
        store.close()

        code, state = self.call("GET", "/api/state")
        self.assertEqual([f["riot_id"] for f in state["friends"]], ["Ayşe#TR1", "Can#TR1"])
        self.assertEqual(state["friends"][0]["winrate"], 80)
        code, state2 = self.call("GET", "/api/state?min=2")
        self.assertEqual(len(state2["friends"]), 2)

        code, r = self.call("POST", "/api/friends", {"puuid": "ayse", "name": "Ayşe"})
        self.assertEqual((code, r["riot_id"]), (200, "Ayşe#TR1"))
        code, state = self.call("GET", "/api/state")
        self.assertEqual([f["riot_id"] for f in state["friends"]], ["Can#TR1"])
        self.assertIn("Ayşe", [p["name"] for p in state["profiles"]])

    def test_create_profile_endpoint_is_atomic(self):
        with mock.patch.object(service, "make_client", lambda platform="tr1": AccountClient()):
            code, err = self.call("POST", "/api/profiles", {"name": "Oqila", "riot_id": "Yok#1"})
            self.assertEqual(code, 400)
            self.assertIn("Hesap bulunamadı", err["error"])
            code, r = self.call("POST", "/api/profiles", {"name": "Oqila#4310", "riot_id": ""})
            self.assertEqual((code, r["riot_id"]), (200, "Oqila#4310"))
        code, state = self.call("GET", "/api/state")
        names = [p["name"] for p in state["profiles"]]
        self.assertEqual(names.count("Oqila"), 1)
        self.assertEqual(len(names), 6)  # 5 hazır profil + Oqila; başarısız deneme iz bırakmadı

    def test_key_test_endpoint_reports_expired_key(self):
        expired = AccountClient(status_error=RiotAuthError("API anahtarı geçersiz ya da süresi dolmuş"))
        with mock.patch.object(service, "make_client", lambda platform="tr1": expired):
            code, r = self.call("POST", "/api/key/test")
        self.assertEqual(code, 200)
        self.assertFalse(r["key_ok"])
        self.assertIn("süresi dolmuş", r["key_message"])
        with mock.patch.object(service, "make_client", lambda platform="tr1": AccountClient()):
            code, r = self.call("POST", "/api/key/test")
        self.assertTrue(r["key_ok"])

    def test_non_json_post_is_rejected(self):
        code, _ = self.call("POST", "/api/profiles", {"name": "X"}, content_type="text/plain")
        self.assertEqual(code, 415)


if __name__ == "__main__":
    unittest.main()
