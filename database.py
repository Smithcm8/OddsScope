import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "oddsscope.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")


def _pg_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def get_connection():
    if USE_POSTGRES:
        return _pg_connect()
    connection = sqlite3.connect(SQLITE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_connection()
    cursor = connection.cursor()
    if USE_POSTGRES:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id BIGSERIAL PRIMARY KEY,
                recorded_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                commence_time TEXT,
                away_team TEXT NOT NULL,
                home_team TEXT NOT NULL,
                sportsbook_key TEXT,
                sportsbook_name TEXT NOT NULL,
                away_ml INTEGER,
                home_ml INTEGER,
                away_spread DOUBLE PRECISION,
                away_spread_price INTEGER,
                home_spread DOUBLE PRECISION,
                home_spread_price INTEGER,
                total DOUBLE PRECISION,
                over_price INTEGER,
                under_price INTEGER
            )
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                event_id TEXT NOT NULL,
                commence_time TEXT,
                away_team TEXT NOT NULL,
                home_team TEXT NOT NULL,
                sportsbook_key TEXT,
                sportsbook_name TEXT NOT NULL,
                away_ml INTEGER,
                home_ml INTEGER,
                away_spread REAL,
                away_spread_price INTEGER,
                home_spread REAL,
                home_spread_price INTEGER,
                total REAL,
                over_price INTEGER,
                under_price INTEGER
            )
        """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_event ON odds_history(event_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_recorded ON odds_history(recorded_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_book ON odds_history(sportsbook_key)")
    connection.commit()
    cursor.close()
    connection.close()


def save_odds_snapshot(games):
    connection = get_connection()
    cursor = connection.cursor()
    recorded_at = datetime.now(timezone.utc).isoformat()
    placeholder = "%s" if USE_POSTGRES else "?"
    marks = ", ".join([placeholder] * 16)
    sql = f"""
        INSERT INTO odds_history (
            recorded_at, event_id, commence_time, away_team, home_team,
            sportsbook_key, sportsbook_name, away_ml, home_ml,
            away_spread, away_spread_price, home_spread, home_spread_price,
            total, over_price, under_price
        ) VALUES ({marks})
    """
    rows_added = 0
    for game in games:
        for book in game.get("books", []):
            cursor.execute(sql, (
                recorded_at, game.get("id"), game.get("commence_time"),
                game.get("away"), game.get("home"), book.get("key"), book.get("name"),
                book.get("away_ml"), book.get("home_ml"),
                book.get("away_spread"), book.get("away_spread_price"),
                book.get("home_spread"), book.get("home_spread_price"),
                book.get("total"), book.get("over_price"), book.get("under_price")
            ))
            rows_added += 1
    connection.commit()
    cursor.close()
    connection.close()
    return rows_added


def get_database_stats():
    connection = get_connection()
    cursor = connection.cursor()
    stats = {}
    for key, query in (
        ("rows", "SELECT COUNT(*) FROM odds_history"),
        ("games", "SELECT COUNT(DISTINCT event_id) FROM odds_history"),
        ("sportsbooks", "SELECT COUNT(DISTINCT sportsbook_key) FROM odds_history"),
    ):
        cursor.execute(query)
        stats[key] = int(cursor.fetchone()[0])
    cursor.close()
    connection.close()
    return stats


def get_market_history(event_id, column, hours):
    """Return timestamp/value rows for one market from SQLite or persistent Postgres."""
    allowed = {"away_spread", "total", "away_ml"}
    if column not in allowed:
        raise ValueError("Unsupported history column")
    connection = get_connection()
    cursor = connection.cursor()
    if USE_POSTGRES:
        cursor.execute(
            f"""SELECT recorded_at, sportsbook_name, {column}
                FROM odds_history
                WHERE event_id = %s AND {column} IS NOT NULL
                  AND recorded_at::timestamptz >= NOW() - (%s * INTERVAL '1 hour')
                ORDER BY recorded_at::timestamptz ASC""",
            (event_id, int(hours))
        )
    else:
        cursor.execute(
            f"""SELECT recorded_at, sportsbook_name, {column}
                FROM odds_history
                WHERE event_id = ? AND {column} IS NOT NULL
                  AND datetime(recorded_at) >= datetime('now', ?)
                ORDER BY datetime(recorded_at) ASC""",
            (event_id, f"-{int(hours)} hours")
        )
    rows = cursor.fetchall()
    result = [{"recorded_at": r[0], "sportsbook_name": r[1], "market_value": r[2]} for r in rows]
    cursor.close()
    connection.close()
    return result


def get_game_history(event_id):
    connection = get_connection()
    cursor = connection.cursor()
    placeholder = "%s" if USE_POSTGRES else "?"
    cursor.execute(f"SELECT * FROM odds_history WHERE event_id = {placeholder} ORDER BY recorded_at ASC", (event_id,))
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cursor.close()
    connection.close()
    return rows
