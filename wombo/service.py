"""CLI ve web arayüzünün ortak işlemleri: anahtar, hesap ekleme, veri çekme, havuz."""
import os
import socket
import ssl
import time

from .pool import build_pool
from .riot import RateLimiter, RiotClient, RiotError, fetch_champions, platform_of
from .store import split_roles

DATA_DIR = "data"
DEFAULT_DB = os.path.join(DATA_DIR, "wombo.db")
KEY_FILE = os.path.join(DATA_DIR, "riot_key.txt")

# Aynı süreçteki tüm istemciler Riot hız limitini birlikte paylaşır
_shared_limiter = RateLimiter()


def load_api_key(key_file=KEY_FILE):
    """Önce arayüzden kaydedilen anahtar, yoksa RIOT_API_KEY ortam değişkeni."""
    try:
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    return (os.environ.get("RIOT_API_KEY") or "").strip() or None


def save_api_key(key, key_file=KEY_FILE):
    key = key.strip()
    if not key.startswith("RGAPI-"):
        raise ValueError("Riot API anahtarı 'RGAPI-' ile başlar.")
    os.makedirs(os.path.dirname(os.path.abspath(key_file)), exist_ok=True)
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key)


def make_client(platform="tr1"):
    return RiotClient(api_key=load_api_key(), platform=platform, limiter=_shared_limiter)


def ensure_champions(store, force=False):
    if force or not store.has_champions():
        store.set_champions(*fetch_champions())


def parse_riot_id(text):
    if "#" not in text:
        raise ValueError("Riot ID 'İsim#TAG' biçiminde olmalı, ör. Faker#KR1")
    name, tag = (part.strip() for part in text.strip().rsplit("#", 1))
    if not name or not tag:
        raise ValueError("Riot ID 'İsim#TAG' biçiminde olmalı, ör. Faker#KR1")
    return name, tag


def resolve_account(client, riot_id):
    """Riot ID'yi API'den doğrular: (puuid, doğru yazılmış Riot ID)."""
    game_name, tag = parse_riot_id(riot_id)
    account = client.account_by_riot_id(game_name, tag)
    if account is None:
        raise RiotError(f"Hesap bulunamadı: {riot_id}. İsmi ve # sonrasındaki etiketi oyundaki gibi yaz.")
    return account["puuid"], f"{account['gameName']}#{account['tagLine']}"


def _check_not_taken(store, puuid, riot_id, profile_id=None):
    owner = store.account_owner(puuid)
    if owner is not None and owner["id"] != profile_id:
        raise ValueError(f"{riot_id} zaten {owner['name']} profiline bağlı.")


def add_account(store, client, profile_id, riot_id):
    """Riot ID'yi doğrular ve profile bağlar. Doğrulanmış tam Riot ID'yi döner."""
    puuid, full_id = resolve_account(client, riot_id)
    _check_not_taken(store, puuid, full_id, profile_id)
    store.add_account(puuid, full_id, profile_id)
    return full_id


def split_profile_input(name, riot_id):
    """İsim kutusuna 'Oqila#4310' yazıldıysa: ('Oqila', 'Oqila#4310')."""
    name, riot_id = (name or "").strip(), (riot_id or "").strip()
    if not riot_id and "#" in name:
        riot_id, name = name, name.rsplit("#", 1)[0].strip()
    if not name and riot_id:
        name = riot_id.rsplit("#", 1)[0].strip()
    if not name:
        raise ValueError("Profil adı boş olamaz.")
    return name, riot_id


def create_profile(store, client_factory, name, riot_id=None):
    """Profil oluşturur; Riot ID verilmişse önce doğrular, doğrulanmazsa hiçbir şey kaydetmez.

    Aynı isimde hesabı olmayan bir profil varsa (ör. önceki başarısız deneme) hesap ona bağlanır.
    """
    name, riot_id = split_profile_input(name, riot_id)
    if not riot_id:
        return store.create_profile(name), None
    puuid, full_id = resolve_account(client_factory(), riot_id)
    _check_not_taken(store, puuid, full_id)
    existing = store.find_profile(name)
    if existing is not None and store.accounts(existing["id"]):
        raise ValueError(f"'{name}' adında bir profil zaten var. Hesabı onun Detay panelinden ekle "
                         "ya da farklı bir ad kullan.")
    profile_id = existing["id"] if existing is not None else store.create_profile(name)
    store.add_account(puuid, full_id, profile_id)
    return profile_id, full_id


def check_key(client):
    """Anahtarı ve bağlantıyı Riot'un durum ucu ile dener. Başarısızsa hata fırlatır."""
    client.platform_status()


def describe_error(e):
    """Ağ/SSL/zaman aşımı hatalarını kullanıcıya anlaşılır bir cümleye çevirir."""
    reason = getattr(e, "reason", e)
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return ("SSL sertifika hatası: Python, Riot'un sertifikasını doğrulayamadı. Mac'te Python "
                "klasöründeki 'Install Certificates.command' dosyasını çalıştır; Windows'ta antivirüsün "
                "HTTPS taramasını kapatmayı dene. Ayrıntı: " + text)
    if isinstance(reason, socket.gaierror):
        return "Riot sunucusuna ulaşılamadı: internet bağlantısını kontrol et. Ayrıntı: " + text
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "Riot sunucusu zamanında cevap vermedi, birazdan tekrar dene."
    if isinstance(e, (RiotError, ValueError)):
        return text
    return f"Ağ hatası: {text}"


def participant_riot_id(p):
    name, tag = p.get("riotIdGameName"), p.get("riotIdTagline")
    if name and tag:
        return f"{name}#{tag}"
    return p.get("summonerName") or None


def save_match(store, match_id, puuid, info):
    """Bir maçtan hesabın kendi satırını ve aynı takımdaki 4 oyuncuyu kaydeder."""
    participants = info.get("participants", [])
    me = next((p for p in participants if p.get("puuid") == puuid), None)
    if me is None:
        return
    store.add_match(match_id, puuid, me["championName"], me.get("teamPosition") or None,
                    me["kills"], me["deaths"], me["assists"], me["win"],
                    info["gameCreation"], info["queueId"])
    for p in participants:
        mate = p.get("puuid")
        riot_id = participant_riot_id(p)
        if p.get("teamId") != me.get("teamId") or mate in (puuid, None, "BOT") or not riot_id:
            continue
        store.add_teammate(match_id, puuid, mate, riot_id, p.get("championName"),
                           p.get("teamPosition") or None, p.get("win"),
                           info["gameCreation"], info["queueId"])


def link_suggested_account(store, mate_puuid, profile_name=None, profile_id=None):
    """Arkadaş önerisini API çağrısı yapmadan yeni ya da mevcut bir profile bağlar."""
    riot_id = store.mate_riot_id(mate_puuid)
    if riot_id is None:
        raise KeyError("Bu oyuncu maç verisinde bulunamadı.")
    if profile_id is None:
        name = (profile_name or riot_id.split("#")[0]).strip()
        if not name:
            raise ValueError("Profil adı boş olamaz.")
        profile_id = store.create_profile(name)
    else:
        profile_id = store.profile(int(profile_id))["id"]
    store.add_account(mate_puuid, riot_id, profile_id)
    return profile_id, riot_id


def update_account(client, store, account, max_matches=100, progress=None):
    """Hesabın yeni maçlarını ve mastery verisini çeker. progress(yapılan, toplam) çağrılır."""
    puuid = account["puuid"]
    known = store.known_match_ids(puuid)
    new_ids = []
    platform = None
    for start in range(0, max_matches, 100):
        ids = client.match_ids(puuid, start=start, count=min(100, max_matches - start))
        platform = platform or next((p for p in map(platform_of, ids) if p), None)
        fresh = [i for i in ids if i not in known]
        new_ids += fresh
        if len(ids) < 100 or len(fresh) < len(ids):
            break  # geçmişin sonuna ya da zaten bilinen maçlara ulaştık
    if progress:
        progress(0, len(new_ids))
    for n, match_id in enumerate(new_ids, 1):
        match = client.match(match_id)
        info = (match or {}).get("info", {})
        if info:
            save_match(store, match_id, puuid, info)
        if n % 10 == 0:
            store.db.commit()
        if progress:
            progress(n, len(new_ids))
    store.db.commit()
    # Mastery sunucuya özeldir; hesabın sunucusu maç ID'sinden (TR1_...) anlaşılır
    rows = [(store.champion_by_numeric_id(m["championId"]), m["championPoints"],
             m["championLevel"], m.get("lastPlayTime")) for m in client.masteries(puuid, platform)]
    store.set_mastery(puuid, rows)
    store.mark_updated(puuid, int(time.time()))
    return len(new_ids)


def load_pool(store, profile):
    pid = profile["id"]
    return build_pool(store.profile_matches(pid), store.profile_mastery(pid), store.extras(pid),
                      name=profile["name"],
                      riot_ids=[a["riot_id"] for a in store.accounts(pid)],
                      preferred_roles=split_roles(profile["preferred_roles"]),
                      avoided_roles=split_roles(profile["avoided_roles"]),
                      note=profile["note"])
