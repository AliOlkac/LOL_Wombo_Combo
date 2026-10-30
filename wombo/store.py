"""SQLite önbelleği: oyuncular, maçlar, mastery ve elle eklenen şampiyonlar."""
import os
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    riot_id TEXT NOT NULL,
    nickname TEXT NOT NULL UNIQUE,
    last_update INTEGER
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
    PRIMARY KEY (match_id, puuid)
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
    puuid TEXT NOT NULL,
    champion TEXT NOT NULL,
    tier TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (puuid, champion)
);
CREATE TABLE IF NOT EXISTS champions (
    numeric_id INTEGER PRIMARY KEY,
    ddid TEXT NOT NULL,
    name TEXT NOT NULL
);
"""


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- oyuncular ---
    def add_player(self, puuid, riot_id, nickname):
        self.db.execute(
            "INSERT INTO players (puuid, riot_id, nickname) VALUES (?, ?, ?) "
            "ON CONFLICT(puuid) DO UPDATE SET riot_id = excluded.riot_id, nickname = excluded.nickname",
            (puuid, riot_id, nickname))
        self.db.commit()

    def players(self):
        return self.db.execute("SELECT * FROM players ORDER BY nickname").fetchall()

    def player(self, nickname):
        row = self.db.execute("SELECT * FROM players WHERE lower(nickname) = lower(?)",
                              (nickname,)).fetchone()
        if row is None:
            raise KeyError(f"Oyuncu bulunamadı: {nickname}")
        return row

    def mark_updated(self, puuid, ts):
        self.db.execute("UPDATE players SET last_update = ? WHERE puuid = ?", (ts, puuid))
        self.db.commit()

    # --- maçlar ---
    def known_match_ids(self, puuid):
        return {r[0] for r in self.db.execute("SELECT match_id FROM matches WHERE puuid = ?", (puuid,))}

    def add_match(self, match_id, puuid, champion, position, kills, deaths, assists, win,
                  game_creation, queue_id):
        self.db.execute(
            "INSERT OR REPLACE INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (match_id, puuid, champion, position, kills, deaths, assists, int(win),
             game_creation, queue_id))

    def matches(self, puuid):
        return self.db.execute("SELECT * FROM matches WHERE puuid = ?", (puuid,)).fetchall()

    # --- mastery ---
    def set_mastery(self, puuid, rows):
        self.db.execute("DELETE FROM mastery WHERE puuid = ?", (puuid,))
        self.db.executemany("INSERT INTO mastery VALUES (?, ?, ?, ?, ?)",
                            [(puuid, *r) for r in rows])
        self.db.commit()

    def mastery(self, puuid):
        return self.db.execute("SELECT * FROM mastery WHERE puuid = ?", (puuid,)).fetchall()

    # --- elle eklenen şampiyonlar ---
    def set_extra(self, puuid, champion, tier, note=None):
        self.db.execute("INSERT OR REPLACE INTO extras VALUES (?, ?, ?, ?)",
                        (puuid, champion, tier, note))
        self.db.commit()

    def remove_extra(self, puuid, champion):
        self.db.execute("DELETE FROM extras WHERE puuid = ? AND champion = ?", (puuid, champion))
        self.db.commit()

    def extras(self, puuid):
        return self.db.execute("SELECT * FROM extras WHERE puuid = ?", (puuid,)).fetchall()

    # --- şampiyon sözlüğü ---
    def set_champions(self, rows):
        self.db.execute("DELETE FROM champions")
        self.db.executemany("INSERT INTO champions VALUES (?, ?, ?)", rows)
        self.db.commit()

    def has_champions(self):
        return self.db.execute("SELECT 1 FROM champions LIMIT 1").fetchone() is not None

    def champion_by_numeric_id(self, numeric_id):
        row = self.db.execute("SELECT ddid FROM champions WHERE numeric_id = ?",
                              (numeric_id,)).fetchone()
        return row[0] if row else str(numeric_id)

    def champion_names(self):
        """ddragon_id (normalize) -> görünen ad."""
        return {_norm(r["ddid"]): r["name"] for r in self.db.execute("SELECT * FROM champions")}

    def find_champion(self, text):
        """Kullanıcının yazdığı adı ('wukong', 'Kai'Sa', 'MonkeyKing') ddragon id'ye çevirir."""
        key = _norm(text)
        for r in self.db.execute("SELECT * FROM champions"):
            if key in (_norm(r["ddid"]), _norm(r["name"])):
                return r["ddid"]
        return None
