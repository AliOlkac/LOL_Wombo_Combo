"""Komut satırı: python -m wombo <komut> ..."""
import argparse
import sys
import time

from . import service
from .pool import POSITION_NAMES, TIER_ALIASES, parse_roles
from .prompt import MODES, player_section
from .riot import RiotError
from .store import Store, split_roles


def cmd_add(args, store):
    client = service.make_client(args.sunucu)
    existing = store.find_profile(args.profil)
    if existing is not None:  # mevcut profile ikinci hesap (smurf)
        riot_id = service.add_account(store, client, existing["id"], args.riot_id)
    else:
        _, riot_id = service.create_profile(store, lambda: client, args.profil, args.riot_id)
    print(f"{args.profil} profiline eklendi: {riot_id}")
    print(f"Şimdi verisini çek: python -m wombo guncelle \"{args.profil}\"")


def cmd_update(args, store):
    client = service.make_client(args.sunucu)
    print("Şampiyon listesi güncelleniyor...")
    service.ensure_champions(store, force=True)
    profiles = [store.profile(n) for n in args.profiller] if args.profiller else store.profiles()
    accounts = [a for p in profiles for a in store.accounts(p["id"])]
    if not accounts:
        sys.exit("Henüz hesap yok. Önce: python -m wombo ekle Profil 'İsim#TAG'")

    def progress(done, total):
        if total and (done % 20 == 0 or done == total):
            print(f"    {done}/{total}")

    for acc in accounts:
        print(f"{acc['riot_id']} güncelleniyor...")
        n = service.update_account(client, store, acc, args.mac, progress)
        print(f"  {n} yeni maç")
    print("Tamam.")


def cmd_list(args, store):
    profiles = store.profiles()
    if not profiles:
        print("Henüz profil yok.")
    for p in profiles:
        print(p["name"])
        for a in store.accounts(p["id"]):
            updated = time.strftime("%d.%m.%Y %H:%M", time.localtime(a["last_update"])) \
                if a["last_update"] else "hiç"
            print(f"    {a['riot_id']:<28} son güncelleme: {updated}")


def cmd_friends(args, store):
    friends = store.suggest_friends(args.min)
    if args.ekle is not None:
        if not 1 <= args.ekle <= len(friends):
            sys.exit(f"1 ile {len(friends)} arasında bir numara ver.")
        f = friends[args.ekle - 1]
        profile_id = store.profile(args.profil)["id"] if args.profil else None
        pid, riot_id = service.link_suggested_account(store, f["puuid"], args.ad, profile_id)
        print(f"{riot_id} → {store.profile(pid)['name']} profiline eklendi. Şimdi güncelle.")
        return
    if not friends:
        print("Öneri yok. Önce kendi hesabını ekleyip güncelle: python -m wombo guncelle")
        return
    print(f"Hesaplarınla aynı takımda en az {args.min} maç oynayanlar:\n")
    for i, f in enumerate(friends, 1):
        wr = round(100 * f["wins"] / f["games"])
        champs = ", ".join(c for c in f["champions"] if c)
        print(f"{i:>2}. {f['riot_id']:<28} {f['games']:>3} maç  %{wr:<3} {champs}")
    print("\nEklemek için: python -m wombo arkadaslar --ekle 1 --ad Ali")
    print("Mevcut profile smurf olarak: python -m wombo arkadaslar --ekle 1 --profil Ali")


def cmd_pool(args, store):
    pool = service.load_pool(store, store.profile(args.profil))
    print(player_section(pool, store.champion_names()))


def cmd_roles(args, store):
    profile = store.profile(args.profil)
    store.update_profile(
        profile["id"],
        preferred_roles=",".join(parse_roles(args.tercih)) if args.tercih is not None else None,
        avoided_roles=",".join(parse_roles(args.oynamaz)) if args.oynamaz is not None else None,
        note=args.notu)
    p = store.profile(profile["id"])

    def show(roles):
        return ", ".join(POSITION_NAMES[r] for r in split_roles(roles)) or "-"

    print(f"{p['name']}: tercih {show(p['preferred_roles'])} | oynamaz {show(p['avoided_roles'])}")


def cmd_extra(args, store):
    service.ensure_champions(store)
    profile = store.profile(args.profil)
    champion = store.find_champion(args.sampiyon)
    if champion is None:
        sys.exit(f"Şampiyon bulunamadı: {args.sampiyon}")
    if args.sil:
        store.remove_extra(profile["id"], champion)
        print(f"{profile['name']}: {champion} elle eklenenlerden çıkarıldı.")
        return
    tier = TIER_ALIASES[args.seviye]
    store.set_extra(profile["id"], champion, tier, args.notu)
    print(f"{profile['name']}: {champion} → {tier}")


def cmd_team(args, store):
    if len(args.profiller) != 5:
        sys.exit("Takım için tam 5 profil adı ver.")
    text = service.team_brief(store, [store.profile(n) for n in args.profiller], args.mod)
    if args.cikti:
        with open(args.cikti, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Brief yazıldı: {args.cikti} — içeriğini Claude'a yapıştır.")
    else:
        print(text)


def cmd_key(args, store):
    service.save_api_key(args.anahtar)
    print("Anahtar kaydedildi (data/riot_key.txt, git'e girmez). Test ediliyor...")
    service.check_key(service.make_client(args.sunucu))
    print("Anahtar çalışıyor, Riot'a bağlantı başarılı.")


def cmd_diagnose(args, store):
    print(service.diagnose(store, args.riot_id, args.sunucu))


def cmd_ui(args, store):
    from .web import serve
    store.close()
    serve(args.db, args.sunucu, args.port, open_browser=not args.tarayici_acma)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wombo", description="LoL Wombo Combo takım kurucu")
    parser.add_argument("--db", default=service.DEFAULT_DB, help="SQLite dosyası (varsayılan: data/wombo.db)")
    parser.add_argument("--sunucu", default="tr1", help="Riot sunucusu (varsayılan: tr1)")
    sub = parser.add_subparsers(dest="komut", required=True)

    p = sub.add_parser("arayuz", help="Web arayüzünü aç")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--tarayici-acma", action="store_true", help="Tarayıcıyı otomatik açma")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("anahtar", help="Riot API anahtarını kaydet")
    p.add_argument("anahtar", help="RGAPI-...")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("teshis", help="Anahtar/bağlantı sorunları için rapor (tam anahtarı göstermez)")
    p.add_argument("riot_id", nargs="?", help="Test için bir Riot ID, ör. Oqila#4310")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("ekle", help="Profile Riot hesabı ekle (profil yoksa oluşturulur)")
    p.add_argument("profil", help="Arkadaşın adı, ör. Ali")
    p.add_argument("riot_id", help="İsim#TAG")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("guncelle", help="Maç geçmişi ve mastery verisini çek")
    p.add_argument("profiller", nargs="*", help="Boş bırakılırsa herkes")
    p.add_argument("--mac", type=int, default=100, help="Hesap başına en fazla maç (varsayılan 100)")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("liste", help="Profilleri ve hesapları listele")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("arkadaslar", help="Seninle sık oynayan oyuncuları öner / ekle")
    p.add_argument("--min", type=int, default=3, help="En az kaç maç birlikte (varsayılan 3)")
    p.add_argument("--ekle", type=int, metavar="NO", help="Listedeki numarayı profil olarak ekle")
    p.add_argument("--ad", help="Yeni profilin adı (varsayılan: oyun içi isim)")
    p.add_argument("--profil", help="Yeni profil yerine bu profile hesap olarak ekle")
    p.set_defaults(func=cmd_friends)

    p = sub.add_parser("havuz", help="Bir profilin şampiyon havuzunu göster")
    p.add_argument("profil")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser("rol", help="Tercih edilen / oynanmayan rolleri ayarla")
    p.add_argument("profil")
    p.add_argument("--tercih", help="ör. jg,top (boş string temizler)")
    p.add_argument("--oynamaz", help="ör. sup")
    p.add_argument("--not", dest="notu", help="Profil notu, ör. 'tank sever'")
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("ekstra", help="Profilin havuzuna elle şampiyon ekle/çıkar")
    p.add_argument("profil")
    p.add_argument("sampiyon")
    p.add_argument("--seviye", choices=list(TIER_ALIASES), default="oynayabilir")
    p.add_argument("--not", dest="notu", help="Kısa açıklama, ör. 'eski main'")
    p.add_argument("--sil", action="store_true", help="Elle eklenen şampiyonu kaldır")
    p.set_defaults(func=cmd_extra)

    p = sub.add_parser("takim", help="5 profil için Claude brief'i üret")
    p.add_argument("profiller", nargs="+")
    p.add_argument("-o", "--cikti", help="Brief'i dosyaya yaz")
    p.add_argument("--mod", choices=list(MODES), default="karma",
                   help="karma: ⅓ deneysel (varsayılan), klasik: hepsi sağlam, deneysel: çoğu deneysel")
    p.set_defaults(func=cmd_team)

    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        args.func(args, store)
    except KeyError as e:
        sys.exit(e.args[0] if e.args else "Bulunamadı")
    except (RiotError, ValueError, OSError) as e:
        sys.exit(service.describe_error(e))


if __name__ == "__main__":
    main()
