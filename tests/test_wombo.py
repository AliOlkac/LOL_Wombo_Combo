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
from wombo.prompt import build_prompt, player_section, synergy_section
from wombo.riot import RateLimiter, RiotAuthError, RiotClient, RiotError
from wombo.store import Store
from wombo.synergy import duo_stats, shared_matches

def setUpModule():
    # Birim testleri ağa çıkmasın: Data Dragon'dan ulti özeti çekmek varsayılan olarak "ağ yok" gibi davranır
    def offline(version, ddid):
        raise OSError("testlerde ağ yok")
    patcher = mock.patch.object(service, "fetch_champion_kit", offline)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


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

    def league_entries(self, puuid, platform=None):
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II", "leaguePoints": 45,
                 "wins": 26, "losses": 24}]


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


class SynergyTest(unittest.TestCase):
    """Ali MF (Support) + Lao Jhin (ADC) gibi ikilileri ortak maçlardan bulma."""

    def setUp(self):
        store = self.store = Store(":memory:")
        self.addCleanup(store.close)
        self.ali = store.create_profile("Ali")
        self.lao = store.create_profile("Lao")
        self.civa = store.create_profile("civa")
        store.add_account("ali", "Oqila#4310", self.ali)
        store.add_account("lao", "Lao#AZ", self.lao)
        store.add_account("civa", "civaHg#TR1", self.civa)
        # m0-m3 Ali'nin geçmişinde (Lao yanında Jhin); m0-m1 Lao'nun kendi geçmişinde de var
        for i in range(4):
            win = i != 3
            store.add_match(f"m{i}", "ali", "MissFortune", "UTILITY", 5, 3, 9, win, NOW - i, 420)
            store.add_teammate(f"m{i}", "ali", "lao", "Lao#AZ", "Jhin", "BOTTOM", win, NOW - i, 420)
            if i < 2:
                store.add_match(f"m{i}", "lao", "Jhin", "BOTTOM", 8, 2, 6, win, NOW - i, 420)
                store.add_teammate(f"m{i}", "lao", "ali", "Oqila#4310", "MissFortune", "UTILITY",
                                   win, NOW - i, 420)
        # m4 sadece Lao'nun geçmişinde: Ali ve civa yanında
        store.add_match("m4", "lao", "Jhin", "BOTTOM", 9, 1, 7, True, NOW - 4, 420)
        store.add_teammate("m4", "lao", "ali", "Oqila#4310", "MissFortune", "UTILITY", True, NOW - 4, 420)
        store.add_teammate("m4", "lao", "civa", "civaHg#TR1", "Amumu", "JUNGLE", True, NOW - 4, 420)
        # birlikte ARAM: sayılmaz
        store.add_match("a1", "ali", "Jinx", None, 1, 1, 1, True, NOW, 450)
        store.add_teammate("a1", "ali", "lao", "Lao#AZ", "Lux", None, True, NOW, 450)
        store.db.commit()
        self.shared = shared_matches(store.shared_rows([self.ali, self.lao, self.civa]))

    def test_matches_seen_from_both_accounts_count_once(self):
        self.assertEqual([m.match_id for m in self.shared], ["m0", "m1", "m2", "m3", "m4"])
        top = duo_stats(self.shared)[0]
        self.assertEqual(top, ((self.ali, "MissFortune", "UTILITY", self.lao, "Jhin", "BOTTOM"), 5, 4))

    def test_brief_section(self):
        names = {self.ali: "Ali", self.lao: "Lao", self.civa: "civa"}
        champs = {"missfortune": "Miss Fortune", "jhin": "Jhin", "amumu": "Amumu"}
        text = synergy_section(self.shared, names, champs)
        self.assertIn("- Ali + Lao: 5 maç, %80", text)
        self.assertIn("| Ali Miss Fortune (Support) + Lao Jhin (ADC) | 5 | %80 |", text)
        self.assertIn("- Galibiyet · Ali Miss Fortune (Support), civa Amumu (Jungle), Lao Jhin (ADC)", text)
        self.assertIn("maçı bulunamadı", synergy_section([], names, champs))

    def test_team_brief_includes_section_and_role_counts(self):
        ids = [self.ali, self.lao, self.civa]
        ids += [self.store.create_profile(f"Ek{i}") for i in range(2)]
        text = service.team_brief(self.store, [self.store.profile(i) for i in ids])
        self.assertIn("## Birlikte oynadığınız maçlar", text)
        self.assertIn("| Support 4 |", text)  # Ali'nin MF'si: rol + maç sayısı
        self.assertIn("Meta rol kalıplarına takılma", text)


class DetailedDataTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.pid = self.store.create_profile("Ali")
        self.store.add_account("me", "Oqila#4310", self.pid)

    def test_extract_stats(self):
        p = {"timeCCingOthers": 31, "totalMinionsKilled": 150, "neutralMinionsKilled": 30,
             "challenges": {"teamDamagePercentage": 0.312, "damageTakenOnTeamPercentage": 0.15,
                            "killParticipation": 0.6, "soloKills": 2, "visionScorePerMinute": 0.8}}
        stats = service.extract_stats(p, {"gameDuration": 1800})
        self.assertEqual(stats, {"dmg_share": 0.312, "taken_share": 0.15, "kp": 0.6, "cc_time": 31,
                                 "solo_kills": 2, "cs_pm": 6.0, "vision_pm": 0.8})

    def test_old_database_gets_stats_column(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE matches (match_id TEXT, puuid TEXT, champion TEXT, position TEXT, "
                       "kills INTEGER, deaths INTEGER, assists INTEGER, win INTEGER, game_creation INTEGER, "
                       "queue_id INTEGER, PRIMARY KEY (match_id, puuid))")
            db.execute("INSERT INTO matches VALUES ('m1', 'x', 'Ahri', 'MIDDLE', 1, 1, 1, 1, 0, 420)")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.matches_missing_stats("x", [420]), ["m1"])
            store.close()

    def test_deeper_update_reaches_older_matches_and_backfills_stats(self):
        matches = {f"TR1_{i}": [participant("me", "Oqila#4310", 100, True)] for i in range(250)}
        client = FakeClient(matches)
        calls = []
        client.match = lambda mid, orig=client.match: calls.append(mid) or orig(mid)
        # bu özellikten önce indirilmiş, istatistiksiz bir maç
        self.store.add_match("TR1_0", "me", "Ahri", "MIDDLE", 1, 1, 1, True, NOW, 420)
        acc = self.store.accounts(self.pid)[0]

        service.update_account(client, self.store, acc, max_matches=100)
        self.assertEqual(len(self.store.known_match_ids("me")), 100)
        self.assertEqual(len(calls), 100)  # 99 yeni + istatistiği eksik TR1_0
        self.assertEqual(self.store.matches_missing_stats("me", [420]), [])

        calls.clear()
        service.update_account(client, self.store, acc, max_matches=300)
        self.assertEqual(len(self.store.known_match_ids("me")), 250)  # ilk sayfa bilinse de devam etti
        self.assertEqual(len(calls), 150)

    def test_pool_style_and_aram(self):
        rows = [dict(match("Jhin", True, pos="BOTTOM"), stats=json.dumps({"dmg_share": 0.3, "cc_time": 10})),
                dict(match("Jhin", False, pos="BOTTOM"), stats=json.dumps({"dmg_share": 0.2, "cc_time": 20})),
                dict(match("Seraphine", True, queue=450), stats=None),
                dict(match("Seraphine", True, queue=450), stats=None)]
        pool = build_pool(rows, [{"champion": "Jhin", "points": 200_000, "level": 7}], [], now_ms=NOW)
        self.assertAlmostEqual(pool.style["dmg_share"], 0.25)
        self.assertEqual(pool.aram, [("Seraphine", 2)])
        self.assertAlmostEqual(pool.champions[0].style.averages()["cc_time"], 15)


class BriefModeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ids = seed_team(self.store)

    def brief(self, mode):
        return service.team_brief(self.store, [self.store.profile(i) for i in self.ids], mode)

    def test_modes(self):
        self.assertIn("Yaklaşık üçte biri **deneysel** olsun.", self.brief("karma"))
        self.assertIn("## Deneysel öneri ne demek", self.brief("karma"))
        klasik = self.brief("klasik")
        self.assertNotIn("Deneysel öneri ne demek", klasik)
        self.assertIn("Hepsi sağlam olsun.", klasik)
        self.assertIn("en fazla bir Esneme", klasik)
        self.assertIn("en fazla iki Esneme", self.brief("deneysel"))
        with self.assertRaises(ValueError):
            self.brief("troll")

    def test_style_columns_and_aram_line(self):
        self.store.add_match("x1", "p1", "Ahri", "MIDDLE", 5, 2, 8, True, NOW, 420,
                             {"dmg_share": 0.31, "cc_time": 12.0, "kp": 0.5})
        for i in range(3):
            self.store.add_match(f"ar{i}", "p1", "Seraphine", None, 1, 1, 1, True, NOW, 450)
        text = self.brief("karma")
        self.assertIn("| Hasar% | CC sn |", text)
        self.assertIn("Oyun tarzı (SR maç başı ortalama): takım hasarının %31'i · 12 sn CC · KP %50", text)
        self.assertIn("ARAM'da oynadıkları (havuza sayılmaz, deneysel fikir için): Seraphine 3", text)


class StrongerPromptTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ids = seed_team(self.store)  # yama 15.1.1; Ahri, Malphite, Wukong
        self.store.update_profile(self.ids[0], preferred_roles="TOP", avoided_roles="UTILITY")

    def brief(self, mode="karma"):
        return service.team_brief(self.store, [self.store.profile(i) for i in self.ids], mode)

    def test_structure_data_first_rules_last(self):
        text = self.brief()
        order = [text.index(h) for h in ("# Wombo combo koçluk isteği", "## Oyuncular", "## Rol matrisi",
                                         "## Birlikte oynadığınız maçlar", "# Kurallar", "# İstenen çıktı")]
        self.assertEqual(order, sorted(order))
        self.assertIn("yama 15.1.1", text)
        self.assertIn("**Esnek** = en az 8 Main/Oynayabilir şampiyonu olan oyuncu", text)
        exp = text.index("## Deneysel öneri ne demek")
        self.assertTrue(text.index("# Kurallar") < exp < text.index("# İstenen çıktı"))
        for must in ("Yasuo R yalnızca havaya kalkmış", "5 maçtan az winrate'i kanıt sayma",
                     "**15-20 farklı öneri**", "**yaklaşık\nyarısı tam 5'li comp**", "en az 15 öneri var mı",
                     "| # | Tip | Top | Jungle | Mid | ADC | Support | Sesli komut | Wombo | Obje | Keyif | Not |",
                     "Numaralar iki tablo boyunca devam etsin", "**kısa ve öz**",
                     "| # | Tip | Çekirdek | Sesli komut | Serbest slotlar | Wombo | Obje | Keyif | Not |", "isteğe bağlı: Oyuncu Şampiyon",
                     "en az\n  üçte biri o şampiyon olmadan", "**Öne çıkanlar**", "**Ortak banlar**",
                     "Bir önerinin numarasını yazarsak o zaman ayrıntı ver"):
            self.assertIn(must, text)

    def test_role_matrix(self):
        text = self.brief()
        self.assertIn("| Oyuncu | Top | Jungle | Mid | ADC | Support |", text)
        # Oyuncu0: Ahri mid 10 maç + smurf'te Malphite top 6 maç; top tercih, support oynamaz
        self.assertIn("| Oyuncu0 | ✓ 6 · Malphite | - | 10 · Ahri | - | ✕ - |", text)

    def test_kits_cached_per_patch_and_listed(self):
        calls = []

        def fake_fetch(version, ddid):
            calls.append((version, ddid))
            return {"tags": ["Tank"], "ult_name": "Unstoppable Force",
                    "ult_desc": "Malphite <status>knocks up</status> enemies.<br><br>" + "x " * 200}

        with mock.patch.object(service, "fetch_champion_kit", fake_fetch):
            text = self.brief()
            self.assertEqual(sorted(d for _, d in calls), ["Ahri", "Malphite"])  # yalnızca havuzdakiler
            self.brief()
            self.assertEqual(len(calls), 2)  # önbellekten
            self.store.set_champions("15.2.1", [(62, "MonkeyKing", "Wukong"), (103, "Ahri", "Ahri"),
                                                (54, "Malphite", "Malphite")])
            self.brief()
            self.assertEqual(len(calls), 4)  # yeni yama: yenilendi
        self.assertIn("## Şampiyon ulti özetleri (Data Dragon, yama 15.1.1)", text)
        line = next(l for l in text.splitlines() if l.startswith("- **Malphite**"))
        self.assertTrue(line.startswith("- **Malphite** [Tank] — R Unstoppable Force: Malphite knocks up enemies. x"))
        self.assertTrue(line.endswith("…"))
        self.assertNotIn("<status>", text)
        from wombo.prompt import clean_kit_text
        self.assertEqual(clean_kit_text("Bir <b>hedef</b>.<br>İkinci <i>cümle</i>."), "Bir hedef. İkinci cümle.")

    def test_brief_without_network_has_no_kit_section(self):
        self.assertNotIn("## Şampiyon ulti özetleri", self.brief())


class RankAndNotesTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_best_rank_across_accounts_and_format(self):
        from wombo.pool import best_ranks, format_rank
        entries = [
            {"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "I", "leaguePoints": 10, "wins": 10, "losses": 10},
            {"queueType": "RANKED_SOLO_5x5", "tier": "PLATINUM", "rank": "IV", "leaguePoints": 0, "wins": 30, "losses": 20},
            {"queueType": "RANKED_FLEX_SR", "tier": "MASTER", "rank": "I", "leaguePoints": 120, "wins": 5, "losses": 5},
            {"queueType": "CHERRY", "tier": "GOLD", "rank": "I"},
        ]
        best = best_ranks(entries)
        self.assertEqual(list(best), ["Solo", "Flex"])
        self.assertEqual(format_rank(best["Solo"]), "Platinum IV 0 LP (%60, 50 maç)")
        self.assertEqual(format_rank(best["Flex"], detail=False), "Master")

    def test_update_stores_rank_and_brief_shows_it(self):
        pid = self.store.create_profile("Ali")
        self.store.add_account("me", "Oqila#4310", pid)
        service.update_account(FakeClient(friend_matches()), self.store, self.store.accounts(pid)[0])
        pool = service.load_pool(self.store, self.store.profile(pid))
        self.assertIn("Rank: Solo Gold II 45 LP (%52, 50 maç)", player_section(pool, {}))

    def test_rank_failure_does_not_break_update(self):
        pid = self.store.create_profile("Ali")
        self.store.add_account("me", "Oqila#4310", pid)
        client = FakeClient(friend_matches())

        def forbidden(puuid, platform=None):
            raise RiotAuthError("403")
        client.league_entries = forbidden
        service.update_account(client, self.store, self.store.accounts(pid)[0])
        self.assertIsNotNone(self.store.accounts(pid)[0]["last_update"])

    def test_default_locke_note_seeded_once_and_editable(self):
        self.assertIn("tek atma ve taşıma potansiyeli yüksek", self.store.notes()["Locke"])
        self.store.remove_note("Locke")
        self.store._migrate()  # tekrar açılınca silinen varsayılan geri gelmez
        self.assertNotIn("Locke", self.store.notes())

    def test_brief_includes_notes_for_pool_champions_only(self):
        ids = seed_team(self.store)
        self.store.set_note("Ahri", "Charm ile combo başlatır.")
        self.store.set_note("Malphite", "Bizde tank top.")
        self.store.set_note("Zed", "Kimse oynamıyor.")
        text = service.team_brief(self.store, [self.store.profile(i) for i in ids])
        self.assertIn("## Şampiyon notlarımız (grubun kendi gözlemi)", text)
        self.assertIn("- **Ahri:** Charm ile combo başlatır.", text)
        self.assertNotIn("Kimse oynamıyor", text)
        for must in ("## Puanlama (her öneri için 1-5)", "**Obje:** ejderha ve Baron'u", "**Keyif:**",
                     "en güçlü wombo, en hızlı obje ve en keyifli"):
            self.assertIn(must, text)


class SpecialNoteTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.ids = seed_team(self.store)

    def brief(self, note=None):
        return service.team_brief(self.store, [self.store.profile(i) for i in self.ids], "karma", note)

    def test_note_goes_to_the_top_and_wins_over_rules(self):
        text = self.brief("Bu akşam eğlence modu.\nAli mid istemiyor.")
        self.assertTrue(text.startswith("# Wombo combo koçluk isteği\n\n## Özel notumuz (bu seferki öncelik)\n\n"
                                        "> Bu akşam eğlence modu.\n> Ali mid istemiyor.\n"))
        self.assertIn("özel notu uygula", text)
        self.assertLess(text.index("Özel notumuz"), text.index("## Oyuncular"))

    def test_no_note_no_trace(self):
        for note in (None, "", "   \n "):
            text = self.brief(note)
            self.assertNotIn("Özel notumuz", text)
            self.assertNotIn("özel notu uygula", text)


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

    def test_adding_account_starts_update_and_finds_friends(self):
        class FullClient(FakeClient, AccountClient):
            def __init__(self):
                FakeClient.__init__(self, friend_matches())
                AccountClient.__init__(self)

        with mock.patch.object(service, "make_client", lambda platform="tr1": FullClient()), \
                mock.patch.object(service, "ensure_champions", lambda store, force=False: None), \
                mock.patch.object(service, "load_api_key", lambda key_file=None: FAKE_KEY):
            code, r = self.call("POST", "/api/profiles", {"name": "Oqila", "riot_id": "Oqila#4310"})
            self.assertEqual(code, 200)
            self.assertIn(r["id"], r["job"]["waiting"] or [r["id"]])
            for _ in range(100):  # arka plan işi bitene kadar bekle
                code, job = self.call("GET", "/api/job")
                if not job["running"]:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(job["errors"], [])
            # aynı hesabı tekrar eklemek hata değil
            code, again = self.call("POST", "/api/profiles", {"name": "Oqila", "riot_id": "Oqila#4310"})
            self.assertEqual((code, again["id"]), (200, r["id"]))
        code, state = self.call("GET", "/api/state")
        me = next(p for p in state["profiles"] if p["name"] == "Oqila")
        self.assertEqual(me["sr_games"], 5)
        self.assertEqual([f["riot_id"] for f in state["friends"]], ["Ayşe#TR1", "Can#TR1"])

    def test_update_requests_are_queued_while_running(self):
        from wombo.web import UpdateJob
        job = UpdateJob(self.db, "tr1")
        job.state.update(running=True, waiting=[1, 2])
        job.start([2, 3, 3, 1])  # 1 şu an işleniyor: tekrar sona eklenir; 2 zaten sırada
        self.assertEqual(job.snapshot()["waiting"], [1, 2, 3, 1])
        self.assertEqual(job.snapshot()["count"], 4)

    def test_brief_mode_and_depth_validation(self):
        code, r = self.call("POST", "/api/brief", {"profile_ids": self.ids, "mode": "deneysel"})
        self.assertEqual(code, 200)
        self.assertIn("Çoğu **deneysel** olsun", r["text"])
        code, err = self.call("POST", "/api/brief", {"profile_ids": self.ids, "mode": "x"})
        self.assertEqual(code, 400)
        with mock.patch.object(service, "load_api_key", lambda key_file=None: FAKE_KEY):
            code, err = self.call("POST", "/api/update", {"max_matches": 5000})
        self.assertEqual(code, 400)
        self.assertIn("derinliği", err["error"])

    def test_version_in_state_page_and_brief(self):
        import re
        from wombo import __version__
        with open(os.path.join(os.path.dirname(service.__file__), "static", "index.html"), encoding="utf-8") as f:
            ui_version = re.search(r'const UI_VERSION = "([^"]+)"', f.read()).group(1)
        self.assertEqual(ui_version, __version__)  # sayfa ile kod aynı sürümde olmalı
        code, state = self.call("GET", "/api/state")
        self.assertEqual(state["version"], __version__)
        code, r = self.call("POST", "/api/brief", {"profile_ids": self.ids})
        self.assertIn(f"_Brief formatı v{__version__}_", r["text"])

    def test_brief_with_special_note(self):
        code, r = self.call("POST", "/api/brief", {"profile_ids": self.ids, "note": "Rakip çok poke'lu"})
        self.assertEqual(code, 200)
        self.assertIn("> Rakip çok poke'lu", r["text"].split("## Oyuncular")[0])

    def test_notes_api_and_rank_label(self):
        code, notes = self.call("GET", "/api/notes")
        self.assertIn("Locke", [n["id"] for n in notes])  # varsayılan not
        code, notes = self.call("POST", "/api/notes", {"champion": "wukong", "note": "  R ile  havaya kaldırır "})
        self.assertEqual(code, 200)
        self.assertIn({"id": "MonkeyKing", "name": "Wukong", "note": "R ile havaya kaldırır"}, notes)
        code, err = self.call("POST", "/api/notes", {"champion": "Yok", "note": "x"})
        self.assertEqual(code, 400)
        code, notes = self.call("DELETE", "/api/notes/MonkeyKing")
        self.assertNotIn("MonkeyKing", [n["id"] for n in notes])

        store = Store(self.db)
        store.set_rank("p1", [{"queueType": "RANKED_FLEX_SR", "tier": "SILVER", "rank": "I",
                               "leaguePoints": 3, "wins": 1, "losses": 1}])
        store.close()
        code, state = self.call("GET", "/api/state")
        ranks = {p["name"]: p["rank"] for p in state["profiles"]}
        self.assertEqual((ranks["Oyuncu1"], ranks["Oyuncu2"]), ("Flex Silver I", None))

    def test_non_json_post_is_rejected(self):
        code, _ = self.call("POST", "/api/profiles", {"name": "X"}, content_type="text/plain")
        self.assertEqual(code, 415)


if __name__ == "__main__":
    unittest.main()
