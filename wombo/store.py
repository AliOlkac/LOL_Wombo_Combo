"""SQLite önbelleği: profiller, hesaplar, maçlar, mastery ve elle eklenen şampiyonlar.

Bir profil bir arkadaşı temsil eder; birden fazla Riot hesabı (smurf) olabilir.
"""
import json
import os
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    preferred_roles TEXT NOT NULL DEFAULT '',
    avoided_roles TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS accounts (
    puuid TEXT PRIMARY KEY,
    riot_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    last_update INTEGER,
    rank TEXT                       -- JSON: league-v4 kayıtları (Solo/Flex)
);
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    champion TEXT NOT NULL,
    position TEXT,
    kills INTEGER, deaths INTEGER, assists INTEGER,
    win INTEGER,
    game_creation INTEGER,
    queue_id INTEGER,
    stats TEXT,                     -- JSON: hasar payı, CC süresi vb. (eski kayıtlarda NULL)
    PRIMARY KEY (match_id, puuid)
);
CREATE TABLE IF NOT EXISTS teammates (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,            -- bizim hesabımız
    mate_puuid TEXT NOT NULL,       -- aynı takımdaki oyuncu
    mate_riot_id TEXT NOT NULL,
    champion TEXT,
    position TEXT,
    win INTEGER,
    game_creation INTEGER,
    queue_id INTEGER,
    PRIMARY KEY (match_id, puuid, mate_puuid)
);
CREATE TABLE IF NOT EXISTS mastery (
    puuid TEXT NOT NULL,
    champion TEXT NOT NULL,
    points INTEGER,
    level INTEGER,
    last_play INTEGER,
    PRIMARY KEY (puuid, champion)
);
CREATE TABLE IF NOT EXISTS extras (
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    champion TEXT NOT NULL,
    tier TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (profile_id, champion)
);
CREATE TABLE IF NOT EXISTS champions (
    numeric_id INTEGER PRIMARY KEY,
    ddid TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS champion_kits (
    ddid TEXT PRIMARY KEY,
    version TEXT NOT NULL,          -- hangi yamadan alındı; yama değişince yenilenir
    tags TEXT,                      -- JSON liste: ["Mage", "Support"]
    ult_name TEXT,
    ult_desc TEXT
);
CREATE TABLE IF NOT EXISTS champion_notes (
    ddid TEXT PRIMARY KEY,          -- şampiyon hakkında grubun kendi gözlemi; brief'e girer
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

DEFAULT_NOTES = {
    "Locke": "Çok hızlı sağa sola gidip takım savaşındaki kaosu fırsata çevirir; "
             "tek atma ve taşıma potansiyeli yüksek.",
}


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def split_roles(text):
    return [r for r in (text or "").split(",") if r]


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Eski veritabanlarına sonradan eklenen sütunları ve varsayılan notları ekler."""
        for table, column in (("matches", "stats"), ("accounts", "rank")):
            columns = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        # Varsayılan notlar bir kez eklenir; kullanıcı sonra düzenleyip silebilir
        if self.db.execute("SELECT 1 FROM meta WHERE key = 'default_notes_v1'").fetchone() is None:
            self.db.executemany("INSERT OR IGNORE INTO champion_notes VALUES (?, ?)", DEFAULT_NOTES.items())
            self.db.execute("INSERT INTO meta VALUES ('default_notes_v1', '1')")
        self.db.commit()

    def close(self):
        self.db.close()

    # --- profiller ---
    def create_profile(self, name):
        cur = self.db.execute("INSERT INTO profiles (name) VALUES (?)", (name.strip(),))
        self.db.commit()
        return cur.lastrowid

    def find_profile(self, name):
        return self.db.execute("SELECT * FROM profiles WHERE name = ?", (name.strip(),)).fetchone()

    def get_or_create_profile(self, name):
        row = self.find_profile(name)
        return row["id"] if row else self.create_profile(name)

    def account_owner(self, puuid):
        """Hesabın bağlı olduğu profil (yoksa None)."""
        return self.db.execute(
            "SELECT p.* FROM profiles p JOIN accounts a ON a.profile_id = p.id WHERE a.puuid = ?",
            (puuid,)).fetchone()

    def profiles(self):
        return self.db.execute("SELECT * FROM profiles ORDER BY name").fetchall()

    def profile(self, key):
        """Profili id (int) ya da ad (str, büyük/küçük harf duyarsız) ile getirir."""
        if isinstance(key, int):
            row = self.db.execute("SELECT * FROM profiles WHERE id = ?", (key,)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM profiles WHERE name = ?", (key.strip(),)).fetchone()
        if row is None:
            raise KeyError(f"Profil bulunamadı: {key}")
        return row

    def update_profile(self, profile_id, **fields):
        allowed = {"name", "preferred_roles", "avoided_roles", "note"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self.db.execute(f"UPDATE profiles SET {cols} WHERE id = ?", (*sets.values(), profile_id))
        self.db.commit()

    def delete_profile(self, profile_id):
        for acc in self.accounts(profile_id):
            self.delete_account(acc["puuid"], commit=False)
        self.db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self.db.commit()

    # --- hesaplar ---
    def add_account(self, puuid, riot_id, profile_id):
        self.db.execute(
            "INSERT INTO accounts (puuid, riot_id, profile_id) VALUES (?, ?, ?) "
            "ON CONFLICT(puuid) DO UPDATE SET riot_id = excluded.riot_id, "
            "profile_id = excluded.profile_id",
            (puuid, riot_id, profile_id))
        self.db.commit()

    def accounts(self, profile_id=None):
        if profile_id is None:
            return self.db.execute("SELECT * FROM accounts ORDER BY riot_id").fetchall()
        return self.db.execute("SELECT * FROM accounts WHERE profile_id = ? ORDER BY riot_id",
                               (profile_id,)).fetchall()

    def delete_account(self, puuid, commit=True):
        for table in ("matches", "teammates", "mastery", "accounts"):
            self.db.execute(f"DELETE FROM {table} WHERE puuid = ?", (puuid,))
        if commit:
            self.db.commit()

    def set_rank(self, puuid, entries):
        self.db.execute("UPDATE accounts SET rank = ? WHERE puuid = ?", (json.dumps(entries), puuid))
        self.db.commit()

    def profile_ranks(self, profile_id):
        """Profilin tüm hesaplarının dereceli kayıtları (düz liste)."""
        entries = []
        for r in self.db.execute("SELECT rank FROM accounts WHERE profile_id = ?", (profile_id,)):
            entries += json.loads(r["rank"]) if r["rank"] else []
        return entries

    # --- şampiyon notları ---
    def notes(self):
        return {r["ddid"]: r["note"] for r in self.db.execute("SELECT * FROM champion_notes ORDER BY ddid")}

    def set_note(self, ddid, note):
        self.db.execute("INSERT OR REPLACE INTO champion_notes VALUES (?, ?)", (ddid, note))
        self.db.commit()

    def remove_note(self, ddid):
        self.db.execute("DELETE FROM champion_notes WHERE ddid = ?", (ddid,))
        self.db.commit()

    def mark_updated(self, puuid, ts):
        self.db.execute("UPDATE accounts SET last_update = ? WHERE puuid = ?", (ts, puuid))
        self.db.commit()

    # --- maçlar ---
    def known_match_ids(self, puuid):
        return {r[0] for r in self.db.execute("SELECT match_id FROM matches WHERE puuid = ?", (puuid,))}

    def add_match(self, match_id, puuid, champion, position, kills, deaths, assists, win,
                  game_creation, queue_id, stats=None):
        self.db.execute(
            "INSERT OR REPLACE INTO matches (match_id, puuid, champion, position, kills, deaths, "
            "assists, win, game_creation, queue_id, stats) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, puuid, champion, position, kills, deaths, assists, int(win),
             game_creation, queue_id, json.dumps(stats) if stats else None))

    def matches_missing_stats(self, puuid, queues):
        """Ayrıntılı istatistiği olmayan (bu özellikten önce indirilmiş) maçlar."""
        ph = ",".join("?" * len(queues))
        return [r[0] for r in self.db.execute(
            f"SELECT match_id FROM matches WHERE puuid = ? AND stats IS NULL AND queue_id IN ({ph}) "
            "ORDER BY game_creation DESC", (puuid, *queues))]

    def profile_matches(self, profile_id):
        return self.db.execute(
            "SELECT m.* FROM matches m JOIN accounts a ON a.puuid = m.puuid WHERE a.profile_id = ?",
            (profile_id,)).fetchall()

    # --- takım arkadaşları / arkadaş keşfi ---
    def add_teammate(self, match_id, puuid, mate_puuid, mate_riot_id, champion, position, win,
                     game_creation, queue_id):
        self.db.execute(
            "INSERT OR REPLACE INTO teammates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, puuid, mate_puuid, mate_riot_id, champion, position, int(win),
             game_creation, queue_id))

    def suggest_friends(self, min_games=3, limit=30):
        """Hesaplarımızla aynı takımda en az min_games maç oynamış, henüz eklenmemiş oyuncular.

        Aynı maçı iki hesabımız da görmüşse (ör. sen ve zaten ekli bir arkadaşın) tek sayılır.
        """
        rows = self.db.execute(
            """
            WITH together AS (
                SELECT DISTINCT match_id, mate_puuid, win, game_creation
                FROM teammates
                WHERE mate_puuid NOT IN (SELECT puuid FROM accounts)
            )
            SELECT t.mate_puuid AS puuid,
                   COUNT(*) AS games,
                   SUM(t.win) AS wins,
                   MAX(t.game_creation) AS last_played,
                   (SELECT mate_riot_id FROM teammates x WHERE x.mate_puuid = t.mate_puuid
                    ORDER BY x.game_creation DESC LIMIT 1) AS riot_id
            FROM together t
            GROUP BY t.mate_puuid
            HAVING COUNT(*) >= ?
            ORDER BY games DESC, last_played DESC
            LIMIT ?
            """, (min_games, limit)).fetchall()
        result = []
        for r in rows:
            champs = self.db.execute(
                "SELECT champion, COUNT(DISTINCT match_id) AS n FROM teammates WHERE mate_puuid = ? "
                "GROUP BY champion ORDER BY n DESC LIMIT 3", (r["puuid"],)).fetchall()
            result.append({**dict(r), "champions": [c["champion"] for c in champs]})
        return result

    def shared_rows(self, profile_ids):
        """Profillerin maç satırları: hem kendi geçmişlerinden hem de başka bir hesabımızın
        takım arkadaşı olarak gördüğü maçlardan. Aynı satır iki kaynaktan gelirse bir kez döner."""
        if not profile_ids:
            return []
        ph = ",".join("?" * len(profile_ids))
        return self.db.execute(
            f"""
            SELECT m.match_id, m.game_creation, m.queue_id, m.win, a.profile_id, m.champion, m.position
            FROM matches m JOIN accounts a ON a.puuid = m.puuid
            WHERE a.profile_id IN ({ph})
            UNION
            SELECT t.match_id, t.game_creation, t.queue_id, t.win, a.profile_id, t.champion, t.position
            FROM teammates t JOIN accounts a ON a.puuid = t.mate_puuid
            WHERE a.profile_id IN ({ph})
            """, (*profile_ids, *profile_ids)).fetchall()

    def mate_riot_id(self, mate_puuid):
        row = self.db.execute("SELECT mate_riot_id FROM teammates WHERE mate_puuid = ? "
                              "ORDER BY game_creation DESC LIMIT 1", (mate_puuid,)).fetchone()
        return row[0] if row else None

    # --- mastery ---
    def set_mastery(self, puuid, rows):
        self.db.execute("DELETE FROM mastery WHERE puuid = ?", (puuid,))
        self.db.executemany("INSERT OR REPLACE INTO mastery VALUES (?, ?, ?, ?, ?)",
                            [(puuid, *r) for r in rows])
        self.db.commit()

    def profile_mastery(self, profile_id):
        return self.db.execute(
            "SELECT m.* FROM mastery m JOIN accounts a ON a.puuid = m.puuid WHERE a.profile_id = ?",
            (profile_id,)).fetchall()

    # --- elle eklenen şampiyonlar ---
    def set_extra(self, profile_id, champion, tier, note=None):
        self.db.execute("INSERT OR REPLACE INTO extras VALUES (?, ?, ?, ?)",
                        (profile_id, champion, tier, note))
        self.db.commit()

    def remove_extra(self, profile_id, champion):
        self.db.execute("DELETE FROM extras WHERE profile_id = ? AND champion = ?",
                        (profile_id, champion))
        self.db.commit()

    def extras(self, profile_id):
        return self.db.execute("SELECT * FROM extras WHERE profile_id = ?", (profile_id,)).fetchall()

    # --- şampiyon sözlüğü ---
    def set_champions(self, version, rows):
        self.db.execute("DELETE FROM champions")
        self.db.executemany("INSERT INTO champions VALUES (?, ?, ?)", rows)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('ddragon_version', ?)", (version,))
        self.db.commit()

    def ddragon_version(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'ddragon_version'").fetchone()
        return row[0] if row else None

    def has_champions(self):
        return self.db.execute("SELECT 1 FROM champions LIMIT 1").fetchone() is not None

    def champion_by_numeric_id(self, numeric_id):
        row = self.db.execute("SELECT ddid FROM champions WHERE numeric_id = ?",
                              (numeric_id,)).fetchone()
        return row[0] if row else str(numeric_id)

    def kits(self, ddids):
        """ddragon_id -> {version, tags, ult_name, ult_desc} (önbellekte olanlar)."""
        if not ddids:
            return {}
        ph = ",".join("?" * len(ddids))
        rows = self.db.execute(f"SELECT * FROM champion_kits WHERE ddid IN ({ph})", list(ddids))
        return {r["ddid"]: {"version": r["version"], "tags": json.loads(r["tags"] or "[]"),
                            "ult_name": r["ult_name"], "ult_desc": r["ult_desc"]} for r in rows}

    def set_kit(self, ddid, version, kit):
        self.db.execute("INSERT OR REPLACE INTO champion_kits VALUES (?, ?, ?, ?, ?)",
                        (ddid, version, json.dumps(kit.get("tags", [])), kit.get("ult_name"),
                         kit.get("ult_desc")))
        self.db.commit()

    def all_champion_ids(self):
        return [r[0] for r in self.db.execute("SELECT ddid FROM champions ORDER BY ddid")]

    def champion_index(self):
        """normalize edilmiş ad -> (ddragon_id, görünen ad)."""
        return {_norm(r["ddid"]): (r["ddid"], r["name"])
                for r in self.db.execute("SELECT * FROM champions")}

    def champion_names(self):
        return {k: name for k, (_, name) in self.champion_index().items()}

    def find_champion(self, text):
        """Kullanıcının yazdığı adı ('wukong', 'Kai'Sa', 'MonkeyKing') ddragon id'ye çevirir."""
        key = _norm(text)
        for r in self.db.execute("SELECT * FROM champions"):
            if key in (_norm(r["ddid"]), _norm(r["name"])):
                return r["ddid"]
        return None
