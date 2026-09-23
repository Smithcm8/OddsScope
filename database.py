import sqlite3
from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "oddsscope.db"


def get_connection():
    """
    Open a connection to the OddsScope database.
    """
    connection = sqlite3.connect(DB_PATH)

    # Allows rows to behave somewhat like dictionaries.
    connection.row_factory = sqlite3.Row

    return connection


def initialize_database():
    """
    Create the database tables if they do not already exist.
    """

    connection = get_connection()
    cursor = connection.cursor()

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

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_event
        ON odds_history(event_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_recorded
        ON odds_history(recorded_at)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_book
        ON odds_history(sportsbook_key)
    """)

    connection.commit()
    connection.close()


def save_odds_snapshot(games):
    """
    Save one snapshot of all currently parsed sportsbook odds.
    """

    connection = get_connection()
    cursor = connection.cursor()

    recorded_at = datetime.now(
        timezone.utc
    ).isoformat()

    rows_added = 0

    for game in games:

        for book in game.get("books", []):

            cursor.execute("""
                INSERT INTO odds_history (

                    recorded_at,

                    event_id,
                    commence_time,

                    away_team,
                    home_team,

                    sportsbook_key,
                    sportsbook_name,

                    away_ml,
                    home_ml,

                    away_spread,
                    away_spread_price,

                    home_spread,
                    home_spread_price,

                    total,
                    over_price,
                    under_price

                )

                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (

                recorded_at,

                game.get("id"),
                game.get("commence_time"),

                game.get("away"),
                game.get("home"),

                book.get("key"),
                book.get("name"),

                book.get("away_ml"),
                book.get("home_ml"),

                book.get("away_spread"),
                book.get("away_spread_price"),

                book.get("home_spread"),
                book.get("home_spread_price"),

                book.get("total"),
                book.get("over_price"),
                book.get("under_price")
            ))

            rows_added += 1

    connection.commit()
    connection.close()

    return rows_added


def get_database_stats():
    """
    Useful for verifying that OddsScope is actually
    accumulating historical data.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT COUNT(*) AS count
        FROM odds_history
    """)

    snapshot_rows = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(DISTINCT event_id) AS count
        FROM odds_history
    """)

    unique_games = cursor.fetchone()["count"]

    cursor.execute("""
        SELECT COUNT(DISTINCT sportsbook_key) AS count
        FROM odds_history
    """)

    unique_books = cursor.fetchone()["count"]

    connection.close()

    return {
        "rows": snapshot_rows,
        "games": unique_games,
        "sportsbooks": unique_books
    }


def get_game_history(event_id):
    """
    Retrieve historical sportsbook observations for one game.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM odds_history
        WHERE event_id = ?
        ORDER BY recorded_at ASC
    """, (event_id,))

    rows = cursor.fetchall()

    connection.close()

    return [
        dict(row)
        for row in rows
    ]