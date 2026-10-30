"""Yerel web arayüzü: python -m wombo arayuz

Yalnızca 127.0.0.1'den dinler. Standart kütüphane dışında bağımlılık yoktur.
"""
import json
import os
import re
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, service
from .pool import POSITION_NAMES, TIER_ALIASES, TIER_ORDER, format_rank, parse_roles
from .riot import RiotAuthError, RiotError
from .store import Store, _norm

STATIC = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_DEPTH = 100
DEPTHS = (100, 200, 300)  # hesap başına çekilecek maç sayısı seçenekleri


class UpdateJob:
    """Arka planda veri çekme işi. Yeni istekler sıraya eklenir; profiller tek tek işlenir."""

    def __init__(self, db_path, platform):
        self.db_path = db_path
        self.platform = platform
        self.lock = threading.Lock()
        self.state = self._fresh(running=False)

    @staticmethod
    def _fresh(running):
        # waiting: işlenmeyi bekleyen profil id'leri (ilk eleman = şu an işlenen)
        return {"running": running, "message": "", "account": "", "profile_id": None,
                "done": 0, "total": 0, "completed": 0, "waiting": [], "errors": [],
                "finished_at": None, "max_matches": DEFAULT_DEPTH}

    def snapshot(self):
        with self.lock:
            return {**self.state, "errors": list(self.state["errors"]),
                    "waiting": list(self.state["waiting"]),
                    "count": self.state["completed"] + len(self.state["waiting"])}

    def _set(self, **fields):
        with self.lock:
            self.state.update(fields)

    def _error(self, text):
        with self.lock:
            self.state["errors"].append(text)

    def start(self, profile_ids, max_matches=None):
        """Profilleri sıraya ekler; iş çalışmıyorsa başlatır. max_matches: hesap başına maç derinliği."""
        depth = max_matches or DEFAULT_DEPTH
        with self.lock:
            if self.state["running"]:
                # şu an işlenen profil tekrar istendiyse (ör. yeni hesap eklendi) sona yeniden eklenir
                queued = self.state["waiting"][1:]
                self.state["waiting"] += [i for i in dict.fromkeys(profile_ids) if i not in queued]
                self.state["max_matches"] = max(self.state["max_matches"], depth)
                return
            self.state = self._fresh(running=True)
            self.state["message"] = "Başlıyor"
            self.state["waiting"] = list(dict.fromkeys(profile_ids))
            self.state["max_matches"] = depth
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        store = Store(self.db_path)
        try:
            client = service.make_client(self.platform)
            self._set(message="Şampiyon listesi indiriliyor")
            service.ensure_champions(store, force=True)
            while True:
                with self.lock:
                    if not self.state["waiting"]:
                        break
                    pid = self.state["waiting"][0]
                try:
                    profile = store.profile(pid)
                except KeyError:  # bu arada silinmiş
                    profile = None
                for acc in store.accounts(pid) if profile else []:
                    self._set(profile_id=pid, account=f"{profile['name']} · {acc['riot_id']}",
                              done=0, total=0, message="Maç listesi alınıyor")
                    try:
                        service.update_account(
                            client, store, acc, max_matches=self.snapshot()["max_matches"],
                            progress=lambda d, t: self._set(done=d, total=t, message="Maçlar indiriliyor"))
                    except RiotAuthError:
                        raise
                    except (RiotError, OSError) as e:
                        self._error(f"{acc['riot_id']}: {service.describe_error(e)}")
                with self.lock:
                    self.state["waiting"].pop(0)
                    self.state["completed"] += 1
            # Brief'teki ulti özetleri için; yama başına bir kez, sonrakiler anında geçer
            self._set(message="Şampiyon ulti özetleri indiriliyor (yama başına bir kez)", account="",
                      profile_id=None, done=0, total=0)
            service.ensure_kits(store, store.all_champion_ids())
            self._set(message="Tamamlandı")
        except Exception as e:  # iş parçacığı sessizce ölmesin, arayüzde görünsün
            self._set(message="Hata")
            self._error(service.describe_error(e))
        finally:
            store.close()
            self._set(running=False, profile_id=None, waiting=[], finished_at=time.time())


def queue_update(h, profile_id):
    """Hesap eklendikten sonra o profilin verisini otomatik çekmeye başlar."""
    if service.load_api_key():
        depth = int(h.body().get("max_matches") or DEFAULT_DEPTH)
        h.server.job.start([profile_id], depth if depth in DEPTHS else DEFAULT_DEPTH)
    return h.server.job.snapshot()


# --- JSON dönüştürücüler ---

def champ_json(c, index):
    ddid, name = index.get(_norm(c.champion), (c.champion, c.champion))
    return {
        "id": ddid, "name": name, "tier": c.tier, "score": round(c.score),
        "games": c.games,
        "winrate": round(c.winrate * 100) if c.games else None,
        "kda": round(c.kda, 1) if c.games else None,
        "roles": [p for p, _ in c.positions.most_common(2)],
        "mastery": c.mastery_points, "manual": bool(c.extra_tier), "note": c.note,
    }


def profile_json(store, profile, index, full=False):
    pool = service.load_pool(store, profile)
    counts = {tier: 0 for tier in TIER_ORDER}
    for c in pool.champions:
        counts[c.tier] += 1
    champs = pool.champions if full else pool.champions[:8]
    return {
        "id": profile["id"], "name": pool.name, "note": pool.note,
        "preferred_roles": pool.preferred_roles, "avoided_roles": pool.avoided_roles,
        "accounts": [{"puuid": a["puuid"], "riot_id": a["riot_id"], "last_update": a["last_update"]}
                     for a in store.accounts(profile["id"])],
        "role_share": [[pos, round(share * 100)] for pos, share in pool.role_share.items()],
        "sr_games": pool.sr_games, "breadth": pool.breadth, "flexible": pool.flexible,
        "rank": rank_label(pool.ranks),
        "counts": counts, "champions": [champ_json(c, index) for c in champs],
    }


def rank_label(ranks):
    """Kart için kısa rank: önce Solo, yoksa Flex."""
    if "Solo" in ranks:
        return format_rank(ranks["Solo"], detail=False)
    if "Flex" in ranks:
        return "Flex " + format_rank(ranks["Flex"], detail=False)
    return None


def notes_json(store):
    index = store.champion_index()
    return [{"id": ddid, "name": index.get(_norm(ddid), (ddid, ddid))[1], "note": note}
            for ddid, note in store.notes().items()]


def key_status():
    key = service.load_api_key()
    status = {"has_key": bool(key), "hint": f"…{key[-4:]}" if key else None,
              "masked": service.mask_key(key) if key else None, "age_hours": None}
    if key and os.path.exists(service.KEY_FILE):
        status["age_hours"] = round((time.time() - os.path.getmtime(service.KEY_FILE)) / 3600, 1)
    return status


# --- API uçları ---

def friends_json(store, index, min_games):
    out = []
    for f in store.suggest_friends(min_games):
        champs = [index.get(_norm(c), (c, c)) for c in f["champions"] if c]
        out.append({
            "puuid": f["puuid"], "riot_id": f["riot_id"], "games": f["games"],
            "winrate": round(100 * f["wins"] / f["games"]), "last_played": f["last_played"] // 1000,
            "champions": [{"id": ddid, "name": name, "tier": ""} for ddid, name in champs],
        })
    return out


def api_state(h, store):
    index = store.champion_index()
    min_games = max(2, int(h.query().get("min", 3)))
    return {
        "profiles": [profile_json(store, p, index) for p in store.profiles()],
        "friends": friends_json(store, index, min_games),
        "ddragon_version": store.ddragon_version(),
        "key": key_status(),
        "job": h.server.job.snapshot(),
        "positions": POSITION_NAMES,
        "version": __version__,
    }


def api_profile(h, store, pid):
    return profile_json(store, store.profile(int(pid)), store.champion_index(), full=True)


def api_create_profile(h, store):
    data = h.body()
    profile_id, riot_id = service.create_profile(
        store, lambda: service.make_client(h.server.platform), data.get("name"), data.get("riot_id"))
    job = queue_update(h, profile_id) if riot_id else h.server.job.snapshot()
    return {"id": profile_id, "riot_id": riot_id, "job": job}


def api_update_profile(h, store, pid):
    data = h.body()
    profile = store.profile(int(pid))

    def roles(key):
        return ",".join(parse_roles(",".join(data[key]))) if key in data else None

    name = data.get("name")
    if name is not None and not name.strip():
        raise ValueError("Profil adı boş olamaz.")
    store.update_profile(profile["id"], name=name.strip() if name else None,
                         preferred_roles=roles("preferred_roles"),
                         avoided_roles=roles("avoided_roles"), note=data.get("note"))


def api_delete_profile(h, store, pid):
    store.delete_profile(store.profile(int(pid))["id"])


def api_add_account(h, store, pid):
    profile = store.profile(int(pid))
    riot_id = service.add_account(store, service.make_client(h.server.platform), profile["id"],
                                  h.body().get("riot_id") or "")
    return {"riot_id": riot_id, "job": queue_update(h, profile["id"])}


def api_delete_account(h, store, puuid):
    store.delete_account(unquote(puuid))


def api_set_extra(h, store, pid):
    data = h.body()
    profile = store.profile(int(pid))
    service.ensure_champions(store)
    champion = store.find_champion(data.get("champion") or "")
    if champion is None:
        raise ValueError(f"Şampiyon bulunamadı: {data.get('champion')}")
    tier = TIER_ALIASES.get((data.get("tier") or "oynayabilir").lower())
    if tier is None:
        raise ValueError("Seviye main, oynayabilir ya da esneme olmalı.")
    store.set_extra(profile["id"], champion, tier, (data.get("note") or "").strip() or None)


def api_delete_extra(h, store, pid, champion):
    store.remove_extra(store.profile(int(pid))["id"], unquote(champion))


def api_add_friend(h, store):
    data = h.body()
    profile_id, riot_id = service.link_suggested_account(
        store, data.get("puuid") or "", profile_name=data.get("name"),
        profile_id=data.get("profile_id") or None)
    return {"profile_id": profile_id, "riot_id": riot_id, "job": queue_update(h, profile_id)}


def api_start_update(h, store):
    data = h.body()
    ids = [int(i) for i in data.get("profile_ids") or []] or [p["id"] for p in store.profiles()]
    depth = int(data.get("max_matches") or DEFAULT_DEPTH)
    if depth not in DEPTHS:
        raise ValueError(f"Maç derinliği {', '.join(map(str, DEPTHS))} olabilir.")
    if not service.load_api_key():
        raise ValueError("Önce Riot API anahtarını kaydet.")
    h.server.job.start(ids, depth)
    return h.server.job.snapshot()


def api_job(h, store):
    return h.server.job.snapshot()


def api_brief(h, store):
    ids = [int(i) for i in h.body().get("profile_ids") or []]
    if len(ids) != 5 or len(set(ids)) != 5:
        raise ValueError("Brief için tam 5 farklı profil seç.")
    mode = h.body().get("mode") or "karma"
    note = h.body().get("note") or ""
    return {"text": service.team_brief(store, [store.profile(i) for i in ids], mode, note)}


def api_save_key(h, store):
    service.save_api_key(h.body().get("key") or "")
    return {**key_status(), **api_test_key(h, store)}


def api_test_key(h, store):
    """Anahtarı Riot'a karşı dener. Hata fırlatmaz; sonucu {"key_ok", "key_message"} olarak döner."""
    try:
        service.check_key(service.make_client(h.server.platform))
        return {"key_ok": True, "key_message": "Anahtar çalışıyor, Riot'a bağlantı başarılı."}
    except Exception as e:
        return {"key_ok": False, "key_message": service.describe_error(e)}


def api_diagnose(h, store):
    riot_id = (h.body().get("riot_id") or "").strip() or None
    return {"text": service.diagnose(store, riot_id, h.server.platform)}


def api_notes(h, store):
    return notes_json(store)


def api_set_note(h, store):
    data = h.body()
    service.ensure_champions(store)
    champion = store.find_champion(data.get("champion") or "")
    if champion is None:
        raise ValueError(f"Şampiyon bulunamadı: {data.get('champion')}")
    note = " ".join((data.get("note") or "").split())
    if not note:
        raise ValueError("Not boş olamaz.")
    store.set_note(champion, note[:300])
    return notes_json(store)


def api_delete_note(h, store, ddid):
    store.remove_note(unquote(ddid))
    return notes_json(store)


def api_champions(h, store):
    service.ensure_champions(store)
    return sorted((name for _, name in store.champion_index().values()), key=str.lower)


ROUTES = [
    ("GET", r"/api/state", api_state),
    ("GET", r"/api/job", api_job),
    ("GET", r"/api/champions", api_champions),
    ("GET", r"/api/profiles/(\d+)", api_profile),
    ("POST", r"/api/profiles", api_create_profile),
    ("POST", r"/api/profiles/(\d+)", api_update_profile),
    ("DELETE", r"/api/profiles/(\d+)", api_delete_profile),
    ("POST", r"/api/profiles/(\d+)/accounts", api_add_account),
    ("DELETE", r"/api/accounts/([^/]+)", api_delete_account),
    ("POST", r"/api/profiles/(\d+)/extras", api_set_extra),
    ("DELETE", r"/api/profiles/(\d+)/extras/([^/]+)", api_delete_extra),
    ("POST", r"/api/friends", api_add_friend),
    ("POST", r"/api/update", api_start_update),
    ("POST", r"/api/brief", api_brief),
    ("POST", r"/api/key", api_save_key),
    ("POST", r"/api/key/test", api_test_key),
    ("POST", r"/api/diagnose", api_diagnose),
    ("GET", r"/api/notes", api_notes),
    ("POST", r"/api/notes", api_set_note),
    ("DELETE", r"/api/notes/([^/]+)", api_delete_note),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Wombo"

    def log_message(self, *args):
        pass

    def query(self):
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def body(self):
        if not hasattr(self, "_body"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self._body = json.loads(raw) if raw else {}
        return self._body

    def send(self, code, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        path = urlparse(self.path).path
        if method == "GET" and path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        # Başka sitelerin tarayıcı üzerinden istek atamaması için yalnızca JSON kabul edilir
        if method != "GET" and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self.send(415, {"error": "JSON bekleniyor."})
        for route_method, pattern, handler in ROUTES:
            match = re.fullmatch(pattern, path)
            if route_method != method or not match:
                continue
            store = Store(self.server.db_path)
            try:
                result = handler(self, store, *match.groups())
                self.send(200, {"ok": True} if result is None else result)
            except KeyError as e:
                self.send(404, {"error": e.args[0] if e.args else "Bulunamadı"})
            except sqlite3.IntegrityError:
                self.send(400, {"error": "Bu isimde bir profil zaten var."})
            except (ValueError, RiotError) as e:
                self.send(400, {"error": str(e)})
            except (URLError, OSError) as e:
                self.send(502, {"error": service.describe_error(e)})
            except Exception as e:  # sunucu cevap vermeden kapanmasın; hata arayüzde görünsün
                self.send(500, {"error": f"Beklenmeyen hata: {e!r}"})
            finally:
                store.close()
            return
        self.send(404, {"error": "Bulunamadı"})


def make_server(db_path, platform="tr1", port=8765):
    Store(db_path).close()  # şemayı oluştur
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    server.platform = platform
    server.job = UpdateJob(db_path, platform)
    return server


def serve(db_path, platform="tr1", port=8765, open_browser=True):
    server = make_server(db_path, platform, port)
    url = f"http://127.0.0.1:{port}"
    print(f"Wombo Combo v{__version__} — arayüz açık: {url}  (kapatmak için Ctrl+C)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Kapatılıyor.")
    finally:
        server.server_close()
