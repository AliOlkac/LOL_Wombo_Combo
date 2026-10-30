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
from urllib.parse import unquote, urlparse

from . import service
from .pool import POSITION_NAMES, TIER_ALIASES, TIER_ORDER, parse_roles
from .prompt import build_prompt
from .riot import RiotAuthError, RiotError
from .store import Store, _norm

STATIC = os.path.join(os.path.dirname(__file__), "static")


class UpdateJob:
    """Arka planda veri çekme işi. Aynı anda tek iş çalışır."""

    def __init__(self, db_path, platform):
        self.db_path = db_path
        self.platform = platform
        self.lock = threading.Lock()
        self.state = {"running": False, "message": "", "account": "", "done": 0, "total": 0,
                      "index": 0, "count": 0, "errors": [], "finished_at": None}

    def snapshot(self):
        with self.lock:
            return {**self.state, "errors": list(self.state["errors"])}

    def _set(self, **fields):
        with self.lock:
            self.state.update(fields)

    def _error(self, text):
        with self.lock:
            self.state["errors"].append(text)

    def start(self, profile_ids):
        with self.lock:
            if self.state["running"]:
                raise ValueError("Zaten bir güncelleme çalışıyor.")
            self.state = {"running": True, "message": "Başlıyor", "account": "", "done": 0,
                          "total": 0, "index": 0, "count": 0, "errors": [], "finished_at": None}
        threading.Thread(target=self._run, args=(profile_ids,), daemon=True).start()

    def _run(self, profile_ids):
        store = Store(self.db_path)
        try:
            client = service.make_client(self.platform)
            self._set(message="Şampiyon listesi indiriliyor")
            service.ensure_champions(store, force=True)
            profiles = [store.profile(i) for i in profile_ids] if profile_ids else store.profiles()
            accounts = [(p["name"], a) for p in profiles for a in store.accounts(p["id"])]
            self._set(count=len(accounts))
            for i, (name, acc) in enumerate(accounts, 1):
                self._set(index=i, account=f"{name} · {acc['riot_id']}", done=0, total=0,
                          message="Maç listesi alınıyor")
                try:
                    service.update_account(
                        client, store, acc,
                        progress=lambda d, t: self._set(done=d, total=t, message="Maçlar indiriliyor"))
                except RiotAuthError:
                    raise
                except (RiotError, URLError) as e:
                    self._error(f"{acc['riot_id']}: {getattr(e, 'reason', e)}")
            self._set(message="Tamamlandı")
        except URLError as e:
            self._set(message="Hata")
            self._error(f"Ağ hatası: {e.reason}")
        except Exception as e:  # iş parçacığı sessizce ölmesin, arayüzde görünsün
            self._set(message="Hata")
            self._error(str(e))
        finally:
            store.close()
            self._set(running=False, finished_at=time.time())


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
        "counts": counts, "champions": [champ_json(c, index) for c in champs],
    }


def key_status():
    key = service.load_api_key()
    status = {"has_key": bool(key), "hint": f"…{key[-4:]}" if key else None, "age_hours": None}
    if key and os.path.exists(service.KEY_FILE):
        status["age_hours"] = round((time.time() - os.path.getmtime(service.KEY_FILE)) / 3600, 1)
    return status


# --- API uçları ---

def api_state(h, store):
    index = store.champion_index()
    return {
        "profiles": [profile_json(store, p, index) for p in store.profiles()],
        "ddragon_version": store.ddragon_version(),
        "key": key_status(),
        "job": h.server.job.snapshot(),
        "positions": POSITION_NAMES,
    }


def api_profile(h, store, pid):
    return profile_json(store, store.profile(int(pid)), store.champion_index(), full=True)


def api_create_profile(h, store):
    name = (h.body().get("name") or "").strip()
    if not name:
        raise ValueError("Profil adı boş olamaz.")
    return {"id": store.create_profile(name)}


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
    return {"riot_id": riot_id}


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


def api_start_update(h, store):
    ids = [int(i) for i in h.body().get("profile_ids") or []]
    if not service.load_api_key():
        raise ValueError("Önce Riot API anahtarını kaydet.")
    h.server.job.start(ids)
    return h.server.job.snapshot()


def api_job(h, store):
    return h.server.job.snapshot()


def api_brief(h, store):
    ids = [int(i) for i in h.body().get("profile_ids") or []]
    if len(ids) != 5 or len(set(ids)) != 5:
        raise ValueError("Brief için tam 5 farklı profil seç.")
    pools = [service.load_pool(store, store.profile(i)) for i in ids]
    return {"text": build_prompt(pools, store.champion_names())}


def api_save_key(h, store):
    service.save_api_key(h.body().get("key") or "")
    return key_status()


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
    ("POST", r"/api/update", api_start_update),
    ("POST", r"/api/brief", api_brief),
    ("POST", r"/api/key", api_save_key),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Wombo"

    def log_message(self, *args):
        pass

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
            except URLError as e:
                self.send(502, {"error": f"Ağ hatası: {e.reason}"})
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
    print(f"Arayüz açık: {url}  (kapatmak için Ctrl+C)")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Kapatılıyor.")
    finally:
        server.server_close()
