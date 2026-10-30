"""CLI ve web arayüzünün ortak işlemleri: anahtar, hesap ekleme, veri çekme, havuz."""
import os
import platform as _platform
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from .pool import SR_QUEUES, build_pool
from .prompt import MAX_CHAMPS, build_prompt
from .riot import (PLATFORM_TO_REGION, USER_AGENT, RateLimiter, RiotClient, RiotError,
                   fetch_champion_kit, fetch_champions, platform_of)
from .store import _norm, split_roles
from .synergy import shared_matches

DATA_DIR = "data"
DEFAULT_DB = os.path.join(DATA_DIR, "wombo.db")
KEY_FILE = os.path.join(DATA_DIR, "riot_key.txt")

# Aynı süreçteki tüm istemciler Riot hız limitini birlikte paylaşır
_shared_limiter = RateLimiter()


KEY_RE = re.compile(r"RGAPI-[A-Za-z0-9-]+")
KEY_LENGTH = 42  # "RGAPI-" + 36 karakterlik UUID


def clean_key(text):
    """Yapıştırılan metinden anahtarı ayıklar: tırnak, boşluk, 'apikey:' öneki, görünmez
    karakterler ve BOM temizlenir. Anahtar bulunamazsa None."""
    text = re.sub(r"[\s\u200b-\u200f\u2060\ufeff]", "", text or "")
    match = KEY_RE.search(text)
    return match.group(0) if match else None


def key_source(key_file=KEY_FILE):
    """(anahtar, kaynak) — önce arayüzden kaydedilen dosya, yoksa RIOT_API_KEY."""
    try:
        with open(key_file, encoding="utf-8-sig") as f:
            key = clean_key(f.read())
        if key:
            return key, key_file
    except FileNotFoundError:
        pass
    key = clean_key(os.environ.get("RIOT_API_KEY"))
    return (key, "RIOT_API_KEY ortam değişkeni") if key else (None, None)


def load_api_key(key_file=KEY_FILE):
    return key_source(key_file)[0]


def save_api_key(text, key_file=KEY_FILE):
    key = clean_key(text)
    if key is None:
        raise ValueError("Bu bir Riot anahtarına benzemiyor. Portaldaki "
                         "'RGAPI-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' anahtarının tamamını kopyala.")
    if len(key) != KEY_LENGTH:
        raise ValueError(f"Anahtar eksik görünüyor ({len(key)} karakter, {KEY_LENGTH} olmalı). "
                         "Portaldaki anahtarın tamamını kopyala.")
    os.makedirs(os.path.dirname(os.path.abspath(key_file)), exist_ok=True)
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def mask_key(key):
    return f"{key[:10]}…{key[-4:]}" if key else "-"


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
    owner = store.account_owner(puuid)
    if owner is not None and owner["name"].lower() == name.lower():
        return owner["id"], full_id  # zaten ekli: tekrar basmak hata değil
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


def _probe(url, key=None):
    """Tek bir ham istek: (HTTP kodu ya da None, açıklama). Hata fırlatmaz."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if key:
        headers["X-Riot-Token"] = key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            limits = r.headers.get("X-App-Rate-Limit")
            return r.status, "OK" + (f" (uygulama limiti: {limits})" if limits else "")
    except HTTPError as e:
        body = " ".join(e.read(300).decode("utf-8", "replace").split())
        return e.code, body or e.reason
    except Exception as e:
        return None, describe_error(e)


def diagnose(store, riot_id=None, platform="tr1"):
    """Anahtar ve bağlantı sorunlarını bulmak için paylaşılabilir bir rapor (tam anahtarı içermez)."""
    key, source = key_source()
    env_key = clean_key(os.environ.get("RIOT_API_KEY"))
    lines = [
        f"Wombo teşhis · {time.strftime('%Y-%m-%d %H:%M')}",
        f"Python {sys.version.split()[0]} · {_platform.platform()} · {ssl.OPENSSL_VERSION}",
        f"Sunucu: {platform} (bölge: {PLATFORM_TO_REGION.get(platform, '?')})",
    ]
    if key is None:
        lines.append("Anahtar: YOK (ne data/riot_key.txt ne RIOT_API_KEY)")
    else:
        age = ""
        if source == KEY_FILE and os.path.exists(KEY_FILE):
            age = f", {round((time.time() - os.path.getmtime(KEY_FILE)) / 60)} dk önce kaydedildi"
        fmt = "biçim doğru" if len(key) == KEY_LENGTH else f"UYARI: {len(key)} karakter, {KEY_LENGTH} olmalı"
        lines.append(f"Anahtar: {mask_key(key)} · {fmt} · kaynak: {source}{age}")
        if env_key and env_key != key:
            lines.append(f"Not: RIOT_API_KEY ({mask_key(env_key)}) da tanımlı ama kullanılmıyor")

    base = f"https://{platform}.api.riotgames.com/lol/status/v4/platform-data"
    no_key = _probe(base)
    lines.append(f"1) Bağlantı (anahtarsız, 401 beklenir): {no_key[0] or '-'} {no_key[1]}")
    results = [no_key]
    if key:
        status = _probe(base, key)
        lines.append(f"2) Anahtarla durum ucu (lol-status-v4): {status[0] or '-'} {status[1]}")
        results.append(status)
        if riot_id is None:
            first = store.accounts()
            riot_id = first[0]["riot_id"] if first else None
        if riot_id and "#" in riot_id:
            name, tag = riot_id.rsplit("#", 1)
            url = "https://{}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{}/{}".format(
                PLATFORM_TO_REGION.get(platform, "europe"), urllib.parse.quote(name.strip()),
                urllib.parse.quote(tag.strip()))
            account = _probe(url, key)
            lines.append(f"3) Hesap sorgusu (account-v1) {riot_id}: {account[0] or '-'} {account[1]}")
            results.append(account)
        else:
            lines.append("3) Hesap sorgusu: atlandı (test için bir Riot ID gir)")

    lines.append("Yorum: " + _interpret(key, results))
    return "\n".join(lines)


def _interpret(key, results):
    connect = results[0][0]
    if connect is None:
        return "Riot sunucusuna hiç ulaşılamıyor; sorun anahtarda değil, ağda (1. satırdaki hataya bak)."
    if key is None:
        return "Bağlantı var ama kayıtlı anahtar yok. Anahtarı kaydet."
    codes = [code for code, _ in results[1:]]
    if all(c == 200 for c in codes):
        return "Anahtar çalışıyor."
    if 401 in codes:
        return "Riot anahtarı tanımıyor: eksik/yanlış kopyalanmış. Portaldan anahtarın tamamını tekrar kopyala."
    if codes and codes[0] == 403:
        return ("Riot anahtarı reddediyor (403). Anahtar süresi dolmuş ya da eski anahtar kayıtlı olabilir: "
                "portalda Regenerate'e bas, sayfayı yenile, çıkan YENİ anahtarı kaydet ve maskeli halini "
                "(baştaki ve sondaki karakterler) portaldakiyle karşılaştır.")
    if len(codes) > 1 and codes[0] == 200 and codes[1] == 403:
        return "Anahtar geçerli ama hesap ucu yasak (403). Bu raporu Claude'a gönder."
    if len(codes) > 1 and codes[0] == 200 and codes[1] == 404:
        return "Anahtar çalışıyor; bu Riot ID bulunamadı. İsmi ve etiketi oyundaki gibi yaz."
    return "Beklenmeyen sonuç; bu raporu Claude'a gönder."


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


def extract_stats(p, info):
    """Oyun tarzını anlatan ayrıntılar (Riot'un 'challenges' alanından). Eksik alanlar atlanır."""
    ch = p.get("challenges") or {}
    minutes = max((info.get("gameDuration") or 0) / 60, 1)
    stats = {
        "dmg_share": ch.get("teamDamagePercentage"),         # takım hasarındaki pay (0-1)
        "taken_share": ch.get("damageTakenOnTeamPercentage"),  # takımın aldığı hasardaki pay
        "kp": ch.get("killParticipation"),
        "cc_time": p.get("timeCCingOthers"),                  # rakibi CC'lediği süre (sn)
        "immobilize": ch.get("enemyChampionImmobilizations"),
        "solo_kills": ch.get("soloKills"),
        "cs_pm": (p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)) / minutes,
        "obj_dpm": (p["damageDealtToObjectives"] / minutes) if "damageDealtToObjectives" in p else None,
        "vision_pm": ch.get("visionScorePerMinute"),
    }
    return {k: round(v, 3) for k, v in stats.items() if isinstance(v, (int, float))}


def save_match(store, match_id, puuid, info):
    """Bir maçtan hesabın kendi satırını ve aynı takımdaki 4 oyuncuyu kaydeder."""
    participants = info.get("participants", [])
    me = next((p for p in participants if p.get("puuid") == puuid), None)
    if me is None:
        return
    store.add_match(match_id, puuid, me["championName"], me.get("teamPosition") or None,
                    me["kills"], me["deaths"], me["assists"], me["win"],
                    info["gameCreation"], info["queueId"], extract_stats(me, info))
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
    """Hesabın yeni maçlarını ve mastery verisini çeker. progress(yapılan, toplam) çağrılır.

    Ayrıntılı istatistiği olmayan eski SR maçları da bir kez yeniden indirilir.
    """
    puuid = account["puuid"]
    known = store.known_match_ids(puuid)
    new_ids = []
    platform = None
    # Maç ID sayfaları ucuzdur (100 maçta 1 istek); derinlik artırılınca eski maçlara da inmek için
    # bilinen maça rastlayınca durmuyoruz, sadece geçmişin sonunda duruyoruz.
    for start in range(0, max_matches, 100):
        count = min(100, max_matches - start)
        ids = client.match_ids(puuid, start=start, count=count)
        platform = platform or next((p for p in map(platform_of, ids) if p), None)
        new_ids += [i for i in ids if i not in known]
        if len(ids) < count:
            break
    to_fetch = new_ids + store.matches_missing_stats(puuid, sorted(SR_QUEUES))
    if progress:
        progress(0, len(to_fetch))
    for n, match_id in enumerate(to_fetch, 1):
        match = client.match(match_id)
        info = (match or {}).get("info", {})
        if info:
            save_match(store, match_id, puuid, info)
        if n % 10 == 0:
            store.db.commit()
        if progress:
            progress(n, len(to_fetch))
    store.db.commit()
    # Mastery sunucuya özeldir; hesabın sunucusu maç ID'sinden (TR1_...) anlaşılır
    rows = [(store.champion_by_numeric_id(m["championId"]), m["championPoints"],
             m["championLevel"], m.get("lastPlayTime")) for m in client.masteries(puuid, platform)]
    store.set_mastery(puuid, rows)
    try:  # rank yalnızca bağlam; alınamazsa güncelleme yine tamamlansın
        store.set_rank(puuid, client.league_entries(puuid, platform))
    except RiotError:
        pass
    store.mark_updated(puuid, int(time.time()))
    return len(new_ids)


def ensure_kits(store, ddids, limit=None):
    """Şampiyonların ulti özetlerini güncel yama için önbelleğe alır, önbellektekileri döner.

    Ağ yoksa ya da Data Dragon cevap vermezse sessizce eldekilerle devam eder: brief kitsiz de çalışır.
    """
    version = store.ddragon_version()
    have = store.kits(ddids)
    if version:
        missing = [d for d in ddids if have.get(d, {}).get("version") != version]
        for ddid in missing[:limit]:
            try:
                store.set_kit(ddid, version, fetch_champion_kit(version, ddid))
            except OSError:
                break  # ağ yok: kalanları denemeye gerek yok
            except (ValueError, KeyError, IndexError, StopIteration):
                continue  # bu şampiyonun verisi beklenmedik biçimde
        have = store.kits(ddids)
    return have


def team_brief(store, profiles, mode="karma", special_note=None):
    """5 profil için Claude brief'i: havuzlar, rol matrisi, ortak maçlar, ulti özetleri."""
    pools = [load_pool(store, p) for p in profiles]
    shared = shared_matches(store.shared_rows([p["id"] for p in profiles]))
    index = store.champion_index()
    ddids = sorted({index[_norm(c.champion)][0] for pool in pools for c in pool.champions[:MAX_CHAMPS]
                    if _norm(c.champion) in index})
    kits = ensure_kits(store, ddids, limit=60)  # çoğu güncelleme sırasında önceden indirilir
    in_pools = {_norm(d) for d in ddids}
    notes = {d: n for d, n in store.notes().items() if _norm(d) in in_pools}
    return build_prompt(pools, store.champion_names(), shared, {p["id"]: p["name"] for p in profiles},
                        mode=mode, kits=kits, patch=store.ddragon_version(), notes=notes,
                        special_note=special_note)


def load_pool(store, profile):
    pid = profile["id"]
    return build_pool(store.profile_matches(pid), store.profile_mastery(pid), store.extras(pid),
                      name=profile["name"],
                      riot_ids=[a["riot_id"] for a in store.accounts(pid)],
                      preferred_roles=split_roles(profile["preferred_roles"]),
                      avoided_roles=split_roles(profile["avoided_roles"]),
                      rank_entries=store.profile_ranks(pid),
                      note=profile["note"])
