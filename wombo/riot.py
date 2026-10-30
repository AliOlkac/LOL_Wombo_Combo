"""Riot Games API ve Data Dragon istemcisi (yalnızca standart kütüphane)."""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from urllib.error import HTTPError

# Platform (sunucu) -> match-v5 / account-v1 için bölgesel yönlendirme
PLATFORM_TO_REGION = {
    "tr1": "europe", "euw1": "europe", "eun1": "europe", "ru": "europe",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

DDRAGON = "https://ddragon.leagueoflegends.com"

# Bazı ağ geçitleri Python'un varsayılan "Python-urllib" kimliğini engeller; kendimizi tanıtalım
USER_AGENT = "LOL-Wombo-Combo/1.0 (personal tool; Python urllib)"


def riot_message(http_error):
    """Riot hata cevabındaki status.message alanı (yoksa gövdenin başı)."""
    try:
        raw = http_error.read(500).decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        return json.loads(raw).get("status", {}).get("message", "") or raw[:120]
    except (ValueError, AttributeError):
        return " ".join(raw.split())[:120]


class RiotError(Exception):
    pass


class RiotAuthError(RiotError):
    """Anahtar yok, geçersiz ya da süresi dolmuş."""


class RateLimiter:
    """Kayan pencere limitleyici. Varsayılan: geliştirici anahtarı limitleri."""

    def __init__(self, limits=((20, 1.0), (100, 120.0)), clock=time.monotonic, sleep=time.sleep):
        self.limits = limits
        self.calls = deque()
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            self._wait()

    def _wait(self):
        longest = max(w for _, w in self.limits)
        while True:
            now = self.clock()
            while self.calls and now - self.calls[0] >= longest:
                self.calls.popleft()
            delay = 0.0
            for n, window in self.limits:
                recent = [t for t in self.calls if now - t < window]
                if len(recent) >= n:
                    delay = max(delay, window - (now - recent[-n]))
            if delay <= 0:
                self.calls.append(now)
                return
            self.sleep(delay + 0.05)


class RiotClient:
    def __init__(self, api_key=None, platform="tr1", limiter=None):
        self.api_key = api_key or os.environ.get("RIOT_API_KEY")
        if not self.api_key:
            raise RiotAuthError(
                "Riot API anahtarı yok. Arayüzden kaydet ya da RIOT_API_KEY ortam değişkenini tanımla "
                "(anahtar: https://developer.riotgames.com)."
            )
        self.platform = platform.lower()
        if self.platform not in PLATFORM_TO_REGION:
            raise RiotError(f"Bilinmeyen sunucu: {platform}")
        self.region = PLATFORM_TO_REGION[self.platform]
        self.limiter = limiter or RateLimiter()

    def _get(self, host, path, params=None):
        url = f"https://{host}.api.riotgames.com{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            self.limiter.wait()
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.api_key,
                                                       "User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 401:
                    raise RiotAuthError(
                        f"Riot anahtarı tanımadı (HTTP 401: {riot_message(e) or 'Unauthorized'}). "
                        "Anahtar eksik ya da hatalı kopyalanmış olabilir; portaldaki anahtarın tamamını kopyala."
                    ) from e
                if e.code == 403:
                    raise RiotAuthError(
                        f"Riot anahtarı reddetti (HTTP 403: {riot_message(e) or 'Forbidden'}). "
                        "Geliştirici anahtarının süresi 24 saatte dolar: portalda Regenerate'e bas ve "
                        "sayfadaki YENİ anahtarı kaydet."
                    ) from e
                if e.code == 429 or e.code >= 500:
                    retry_after = e.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                    continue
                raise RiotError(f"Riot hatası HTTP {e.code} ({riot_message(e)}): {url}") from e
            except (TimeoutError, ConnectionError):
                if attempt >= 2:
                    raise
                time.sleep(2 ** attempt)
        raise RiotError(f"İstek tekrar denemelere rağmen başarısız: {url}")

    def account_by_riot_id(self, game_name, tag_line):
        path = "/riot/account/v1/accounts/by-riot-id/{}/{}".format(
            urllib.parse.quote(game_name), urllib.parse.quote(tag_line))
        return self._get(self.region, path)

    def match_ids(self, puuid, start=0, count=100):
        return self._get(self.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                         {"start": start, "count": count}) or []

    def match(self, match_id):
        return self._get(self.region, f"/lol/match/v5/matches/{match_id}")

    def platform_status(self):
        """Anahtar gerektiren hafif bir uç; anahtar ve bağlantı testi için."""
        return self._get(self.platform, "/lol/status/v4/platform-data")

    def masteries(self, puuid, platform=None):
        """platform verilmezse istemcinin sunucusu kullanılır (ör. maç ID'sinden 'tr1')."""
        host = platform if platform in PLATFORM_TO_REGION else self.platform
        return self._get(host, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}") or []


def platform_of(match_id):
    """'TR1_1234567' -> 'tr1'. Tanınmayan biçimde None."""
    prefix = match_id.split("_", 1)[0].lower()
    return prefix if prefix in PLATFORM_TO_REGION else None


def fetch_champions():
    """Data Dragon'dan güncel sürüm ve şampiyon listesi: (sürüm, [(sayısal_id, ddragon_id, ad)])."""
    with urllib.request.urlopen(f"{DDRAGON}/api/versions.json", timeout=30) as resp:
        version = json.load(resp)[0]
    with urllib.request.urlopen(f"{DDRAGON}/cdn/{version}/data/en_US/champion.json", timeout=30) as resp:
        data = json.load(resp)["data"]
    return version, [(int(c["key"]), c["id"], c["name"]) for c in data.values()]
