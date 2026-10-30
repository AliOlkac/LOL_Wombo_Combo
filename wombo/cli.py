"""Komut satırı: python -m wombo <komut> ..."""
import argparse
import os
import sys
import time
from urllib.error import URLError

from .pool import TIER_ALIASES, build_pool
from .prompt import build_prompt, player_section
from .riot import RiotClient, RiotError, fetch_champions
from .store import Store

DEFAULT_DB = os.path.join("data", "wombo.db")


def ensure_champions(store, force=False):
    if force or not store.has_champions():
        print("Şampiyon listesi Data Dragon'dan indiriliyor...")
        store.set_champions(fetch_champions())


def load_pool(store, player):
    return build_pool(store.matches(player["puuid"]), store.mastery(player["puuid"]),
                      store.extras(player["puuid"]), player["nickname"], player["riot_id"])


def cmd_add(args, store):
    if "#" not in args.riot_id:
        sys.exit("Riot ID 'İsim#TAG' biçiminde olmalı, ör. Faker#KR1")
    game_name, tag = args.riot_id.rsplit("#", 1)
    client = RiotClient(platform=args.sunucu)
    account = client.account_by_riot_id(game_name, tag)
    if account is None:
        sys.exit(f"Hesap bulunamadı: {args.riot_id}")
    riot_id = f"{account['gameName']}#{account['tagLine']}"
    nickname = args.ad or account["gameName"]
    store.add_player(account["puuid"], riot_id, nickname)
    print(f"Eklendi: {nickname} ({riot_id}). Şimdi: python -m wombo guncelle {nickname}")


def update_player(client, store, player, max_matches):
    puuid = player["puuid"]
    known = store.known_match_ids(puuid)
    new_ids = []
    for start in range(0, max_matches, 100):
        ids = client.match_ids(puuid, start=start, count=min(100, max_matches - start))
        fresh = [i for i in ids if i not in known]
        new_ids += fresh
        if len(ids) < 100 or len(fresh) < len(ids):
            break  # geçmişin sonuna ya da zaten bilinen maçlara ulaştık
    print(f"  {len(new_ids)} yeni maç indiriliyor...")
    for n, match_id in enumerate(new_ids, 1):
        match = client.match(match_id)
        if match is None:
            continue
        info = match["info"]
        me = next((p for p in info["participants"] if p["puuid"] == puuid), None)
        if me is None:
            continue
        store.add_match(match_id, puuid, me["championName"], me.get("teamPosition") or None,
                        me["kills"], me["deaths"], me["assists"], me["win"],
                        info["gameCreation"], info["queueId"])
        if n % 20 == 0:
            store.db.commit()
            print(f"    {n}/{len(new_ids)}")
    store.db.commit()
    rows = [(store.champion_by_numeric_id(m["championId"]), m["championPoints"],
             m["championLevel"], m.get("lastPlayTime")) for m in client.masteries(puuid)]
    store.set_mastery(puuid, rows)
    store.mark_updated(puuid, int(time.time()))


def cmd_update(args, store):
    client = RiotClient(platform=args.sunucu)
    ensure_champions(store, force=True)
    players = [store.player(n) for n in args.adlar] if args.adlar else store.players()
    if not players:
        sys.exit("Henüz oyuncu yok. Önce: python -m wombo ekle 'İsim#TAG'")
    for p in players:
        print(f"{p['nickname']} güncelleniyor...")
        update_player(client, store, p, args.mac)
    print("Tamam.")


def cmd_list(args, store):
    players = store.players()
    if not players:
        print("Henüz oyuncu yok.")
    for p in players:
        updated = time.strftime("%d.%m.%Y %H:%M", time.localtime(p["last_update"])) \
            if p["last_update"] else "hiç"
        print(f"{p['nickname']:<16} {p['riot_id']:<24} son güncelleme: {updated}")


def cmd_pool(args, store):
    pool = load_pool(store, store.player(args.ad))
    print(player_section(pool, store.champion_names()))


def cmd_extra(args, store):
    ensure_champions(store)
    player = store.player(args.ad)
    champion = store.find_champion(args.sampiyon)
    if champion is None:
        sys.exit(f"Şampiyon bulunamadı: {args.sampiyon}")
    if args.sil:
        store.remove_extra(player["puuid"], champion)
        print(f"{player['nickname']}: {champion} elle eklenenlerden çıkarıldı.")
        return
    tier = TIER_ALIASES[args.seviye]
    store.set_extra(player["puuid"], champion, tier, args.notu)
    print(f"{player['nickname']}: {champion} → {tier}")


def cmd_team(args, store):
    if len(args.adlar) != 5:
        sys.exit("Takım için tam 5 oyuncu adı ver.")
    pools = [load_pool(store, store.player(n)) for n in args.adlar]
    text = build_prompt(pools, store.champion_names())
    if args.cikti:
        with open(args.cikti, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Brief yazıldı: {args.cikti} — içeriğini Claude'a yapıştır.")
    else:
        print(text)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wombo", description="LoL Wombo Combo takım kurucu")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite dosyası (varsayılan: data/wombo.db)")
    parser.add_argument("--sunucu", default="tr1", help="Riot sunucusu (varsayılan: tr1)")
    sub = parser.add_subparsers(dest="komut", required=True)

    p = sub.add_parser("ekle", help="Riot ID ile oyuncu ekle")
    p.add_argument("riot_id", help="İsim#TAG")
    p.add_argument("--ad", help="Kısa takma ad (varsayılan: oyun içi isim)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("guncelle", help="Maç geçmişi ve mastery verisini çek")
    p.add_argument("adlar", nargs="*", help="Boş bırakılırsa herkes")
    p.add_argument("--mac", type=int, default=100, help="Oyuncu başına en fazla maç (varsayılan 100)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("liste", help="Oyuncuları listele")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("havuz", help="Bir oyuncunun şampiyon havuzunu göster")
    p.add_argument("ad")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("ekstra", help="Oyuncunun havuzuna elle şampiyon ekle/çıkar")
    p.add_argument("ad")
    p.add_argument("sampiyon")
    p.add_argument("--seviye", choices=list(TIER_ALIASES), default="oynayabilir")
    p.add_argument("--not", dest="notu", help="Kısa açıklama, ör. 'eski main'")
    p.add_argument("--sil", action="store_true", help="Elle eklenen şampiyonu kaldır")
    p.set_defaults(func=cmd_extra)

    p = sub.add_parser("takim", help="5 oyuncu için Claude brief'i üret")
    p.add_argument("adlar", nargs="+")
    p.add_argument("-o", "--cikti", help="Brief'i dosyaya yaz")
    p.set_defaults(func=cmd_team)

    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        args.func(args, store)
    except (RiotError, KeyError) as e:
        sys.exit(str(e).strip("'\""))
    except URLError as e:
        sys.exit(f"Ağ hatası: {e.reason}")


if __name__ == "__main__":
    main()
