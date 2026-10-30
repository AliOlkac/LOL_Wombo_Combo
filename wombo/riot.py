"""Riot Games API ve Data Dragon istemcisi (yalnızca standart kütüphane)."""
import json
import os
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


class RiotError(Exception):
    pass


class RateLimiter:
    """Kayan pencere limitleyici. Varsayılan: geliştirici anahtarı limitleri."""

    def __init__(self, limits=((20, 1.0), (100, 120.0)), clock=time.monotonic, sleep=time.sleep):
        self.limits = limits
        self.calls = deque()
        self.clock = clock
        self.sleep = sleep

    def wait(self):
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
            raise RiotError(
                "RIOT_API_KEY ortam değişkeni tanımlı değil. "
                "Anahtarı https://developer.riotgames.com adresinden alabilirsin."
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
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.api_key})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.load(resp)
            except HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (401, 403):
                    raise RiotError(
                        "API anahtarı geçersiz ya da süresi dolmuş "
                        "(geliştirici anahtarları 24 saatte bir yenilenmeli)."
                    ) from e
                if e.code == 429 or e.code >= 500:
                    retry_after = e.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                    continue
                raise RiotError(f"HTTP {e.code}: {url}") from e
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

    def masteries(self, puuid):
        return self._get(self.platform,
                         f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}") or []


def fetch_champions():
    """Data Dragon'dan şampiyon listesi: [(sayısal_id, ddragon_id, görünen_ad)]."""
    with urllib.request.urlopen(f"{DDRAGON}/api/versions.json", timeout=30) as resp:
        version = json.load(resp)[0]
    with urllib.request.urlopen(f"{DDRAGON}/cdn/{version}/data/en_US/champion.json", timeout=30) as resp:
        data = json.load(resp)["data"]
    return [(int(c["key"]), c["id"], c["name"]) for c in data.values()]
