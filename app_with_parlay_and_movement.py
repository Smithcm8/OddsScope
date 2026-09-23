from flask import Flask, render_template_string, jsonify, request
import requests
import os
import time
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

from database import (
    initialize_database,
    save_odds_snapshot,
    get_database_stats
)

from signals import analyze_games


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

initialize_database()

API_KEY = os.getenv("PROPLINE_API_KEY")

SPORT_KEY = "football_ncaaf"

PROPLINE_URL = (
    f"https://api.prop-line.com/v1/sports/{SPORT_KEY}/odds"
)

MARKETS = "h2h,spreads,totals"

REQUEST_TIMEOUT = 25

# Keep API results for 60 seconds.
# Refreshing the browser during this period will NOT call PropLine again.
CACHE_SECONDS = 60

CACHE = {
    "timestamp": 0,
    "games": [],
    "meta": {}
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def safe_float(value):
    """
    Convert a value to float if possible.
    """

    try:
        if value is None:
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def safe_int(value):
    """
    Convert American odds to int if possible.
    """

    try:
        if value is None:
            return None

        return int(float(value))

    except (TypeError, ValueError):
        return None


def format_american(value):
    """
    Format American odds.

    120  -> +120
    -110 -> -110
    """

    value = safe_int(value)

    if value is None:
        return "—"

    if value > 0:
        return f"+{value}"

    return str(value)


def format_point(value):
    """
    Format spread/total numbers nicely.

    2.0  -> 2
    2.5  -> 2.5
    """

    value = safe_float(value)

    if value is None:
        return "—"

    if value == 0:
        return "PK"

    if value.is_integer():
        return str(int(value))

    return f"{value:g}"


def format_spread(value):
    """
    Format a spread with + / - signs.
    """

    value = safe_float(value)

    if value is None:
        return "—"

    if value == 0:
        return "PK"

    if value > 0:
        return f"+{format_point(value)}"

    return format_point(value)


def american_implied_probability(odds):
    """
    Convert American odds to implied probability.

    Example:
        -110 -> 52.38%
        +150 -> 40.00%
    """

    odds = safe_float(odds)

    if odds is None or odds == 0:
        return None

    if odds < 0:
        return abs(odds) / (abs(odds) + 100)

    return 100 / (odds + 100)


def probability_display(probability):
    """
    Convert decimal probability to percentage string.
    """

    if probability is None:
        return "—"

    return f"{probability * 100:.1f}%"


def calculate_no_vig(away_odds, home_odds):
    """
    Remove sportsbook vig from a two-way moneyline market.
    """

    away_prob = american_implied_probability(away_odds)
    home_prob = american_implied_probability(home_odds)

    if away_prob is None or home_prob is None:
        return None, None, None

    total = away_prob + home_prob

    if total <= 0:
        return None, None, None

    away_no_vig = away_prob / total
    home_no_vig = home_prob / total

    hold = total - 1

    return away_no_vig, home_no_vig, hold


def format_datetime(iso_string):
    """
    Return simple date/time information.

    We keep the raw ISO time as well so JavaScript can convert
    it to the user's browser timezone.
    """

    if not iso_string:
        return {
            "date": "TBD",
            "time": "TBD",
            "iso": ""
        }

    try:

        dt = datetime.fromisoformat(
            iso_string.replace("Z", "+00:00")
        )

        return {
            "date": dt.strftime("%a, %b %d"),
            "time": dt.strftime("%I:%M %p UTC").lstrip("0"),
            "iso": iso_string
        }

    except Exception:

        return {
            "date": "TBD",
            "time": "",
            "iso": iso_string
        }


# ============================================================
# TEAM NAME MATCHING
# ============================================================

def normalize_team_name(name):
    """
    Normalize team names so:

        Liberty
        Liberty Flames

    can still be recognized as the same team.
    """

    if not name:
        return ""

    text = str(name).lower()

    characters = []

    for char in text:

        if char.isalnum() or char.isspace():
            characters.append(char)

    return " ".join(
        "".join(characters).split()
    )


def team_matches(outcome_name, team_name):
    """
    Determine whether an sportsbook outcome belongs to a team.

    Matching must be conservative. False matches are much worse
    than missing a sportsbook because they can create fake odds,
    fake signals, and fake arbitrage opportunities.
    """

    outcome = normalize_team_name(outcome_name)
    team = normalize_team_name(team_name)

    if not outcome or not team:
        return False

    # Perfect normalized match.
    if outcome == team:
        return True

    outcome_words = outcome.split()
    team_words = team.split()

    # Allow one name to contain the complete other name.
    #
    # Example:
    #   "liberty"
    #   "liberty flames"
    #
    # But require at least TWO matching words when the shorter
    # name contains multiple words. This prevents:
    #
    #   "new mexico"
    #   "new mexico st"
    #
    # from both matching simply because they begin with "new".
    if len(outcome_words) == 1:

        return (
            outcome_words[0]
            in team_words
        )

    if len(team_words) == 1:

        return (
            team_words[0]
            in outcome_words
        )

    # Multi-word team names should match exactly here.
    return False

# ============================================================
# MARKET EXTRACTION
# ============================================================

def get_markets(book, market_key):
    """
    Get all markets of a particular type.

    A sportsbook can return multiple 'spreads' or 'totals'
    because alternate lines are included.
    """

    return [
        market
        for market in book.get("markets", [])
        if market.get("key") == market_key
    ]


def get_team_outcome(market, team_name):
    """
    Find a team's outcome inside a market.
    """

    for outcome in market.get("outcomes", []):

        if team_matches(
            outcome.get("name"),
            team_name
        ):
            return outcome

    return None


def get_named_outcome(market, name):
    """
    Find Over / Under outcome.
    """

    wanted = str(name).strip().lower()

    for outcome in market.get("outcomes", []):

        actual = str(
            outcome.get("name", "")
        ).strip().lower()

        if actual == wanted:
            return outcome

    return None


# ============================================================
# MAIN LINE SELECTION
# ============================================================

def price_distance_from_even(odds):
    """
    Main markets usually have both sides priced relatively
    close to even money.

    We use implied probability distance from 50% as part
    of the main-line scoring system.
    """

    probability = american_implied_probability(odds)

    if probability is None:
        return 999

    return abs(probability - 0.5)


def two_way_market_score(price1, price2):
    """
    Score a paired market.

    Lower = more likely to be the sportsbook's main line.

    Example:
        -110 / -110 should beat
        -300 / +250
    """

    p1 = american_implied_probability(price1)
    p2 = american_implied_probability(price2)

    if p1 is None or p2 is None:
        return 999

    # Distance of each side from 50%
    balance = (
        abs(p1 - 0.5)
        +
        abs(p2 - 0.5)
    )

    # Sportsbook overround.
    hold = abs(
        (p1 + p2) - 1
    )

    return balance + (hold * 0.50)


def choose_main_spread(
    book,
    away_team,
    home_team
):
    """
    Select the most likely primary spread from all spread
    alternatives returned by the sportsbook.

    IMPORTANT:
    PropLine can return many alternate spreads.

    We identify candidate paired markets and choose the
    one whose prices are closest to a normal two-way
    sportsbook market.
    """

    candidates = []

    for market in get_markets(book, "spreads"):

        away = get_team_outcome(
            market,
            away_team
        )

        home = get_team_outcome(
            market,
            home_team
        )

        if not away or not home:
            continue

        # Safety check:
        # the away and home teams must resolve to two different
        # sportsbook outcomes.
        away_name = normalize_team_name(
        away.get("name")
            )

        home_name = normalize_team_name(
        home.get("name")
        )

        if away_name == home_name:
            continue
        away_point = safe_float(
            away.get("point")
        )

        home_point = safe_float(
            home.get("point")
        )

        away_price = safe_int(
            away.get("price")
        )

        home_price = safe_int(
            home.get("price")
        )

        if (
            away_point is None
            or home_point is None
            or away_price is None
            or home_price is None
        ):
            continue

        # Valid game spreads should oppose each other.
        if abs(
            away_point + home_point
        ) > 0.01:
            continue

        score = two_way_market_score(
            away_price,
            home_price
        )

        candidates.append({
            "away_point": away_point,
            "away_price": away_price,

            "home_point": home_point,
            "home_price": home_price,

            "score": score
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x["score"]
    )

    return candidates[0]


def choose_main_total(book):
    """
    Select the most likely primary game total.

    Alternate totals can exist at many different points.
    The primary total is usually the paired Over/Under
    market with prices closest to even.
    """

    candidates = []

    for market in get_markets(book, "totals"):

        # Some APIs put team totals and game totals under
        # similar structures. We only want generic Over/Under
        # markets here.
        description = str(
            market.get("description", "")
        ).lower()

        over = get_named_outcome(
            market,
            "Over"
        )

        under = get_named_outcome(
            market,
            "Under"
        )

        if not over or not under:
            continue

        over_point = safe_float(
            over.get("point")
        )

        under_point = safe_float(
            under.get("point")
        )

        over_price = safe_int(
            over.get("price")
        )

        under_price = safe_int(
            under.get("price")
        )

        if (
            over_point is None
            or under_point is None
            or over_price is None
            or under_price is None
        ):
            continue

        # Over and Under must reference the same total.
        if abs(
            over_point - under_point
        ) > 0.01:
            continue

        # Strongly penalize markets whose description
        # clearly looks like a team total.
        description_penalty = 0

        if (
            "team" in description
            or "1st half" in description
            or "first half" in description
            or "quarter" in description
        ):
            description_penalty = 10

        score = (
            two_way_market_score(
                over_price,
                under_price
            )
            +
            description_penalty
        )

        candidates.append({
            "point": over_point,
            "over_price": over_price,
            "under_price": under_price,
            "score": score
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x["score"]
    )

    return candidates[0]


def choose_moneyline(
    book,
    away_team,
    home_team
):
    """
    Select the main head-to-head moneyline.
    """

    candidates = []

    for market in get_markets(book, "h2h"):

        away = get_team_outcome(
            market,
            away_team
        )

        home = get_team_outcome(
            market,
            home_team
        )

        if not away or not home:
            continue

        # Never allow the same sportsbook outcome to represent
        # both teams. This protects games such as New Mexico
        # vs New Mexico State from false moneyline matches.
        away_name = normalize_team_name(
            away.get("name")
        )

        home_name = normalize_team_name(
            home.get("name")
        )

        if away_name == home_name:
            continue

        away_price = safe_int(
            away.get("price")
        )

        home_price = safe_int(
            home.get("price")
        )

        if (
            away_price is None
            or home_price is None
        ):
            continue

        score = two_way_market_score(
            away_price,
            home_price
        )

        candidates.append({
            "away_price": away_price,
            "home_price": home_price,
            "score": score
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x["score"]
    )

    return candidates[0]


# ============================================================
# PARSE ONE SPORTSBOOK
# ============================================================

def parse_book(
    book,
    away_team,
    home_team
):
    """
    Convert raw sportsbook data into the clean structure
    used by our website.
    """

    moneyline = choose_moneyline(
        book,
        away_team,
        home_team
    )

    spread = choose_main_spread(
        book,
        away_team,
        home_team
    )

    total = choose_main_total(
        book
    )


    # --------------------------------------------------------
    # MONEYLINE
    # --------------------------------------------------------

    away_ml = None
    home_ml = None

    if moneyline:

        away_ml = moneyline[
            "away_price"
        ]

        home_ml = moneyline[
            "home_price"
        ]


    # --------------------------------------------------------
    # SPREAD
    # --------------------------------------------------------

    away_spread = None
    away_spread_price = None

    home_spread = None
    home_spread_price = None

    if spread:

        away_spread = spread[
            "away_point"
        ]

        away_spread_price = spread[
            "away_price"
        ]

        home_spread = spread[
            "home_point"
        ]

        home_spread_price = spread[
            "home_price"
        ]


    # --------------------------------------------------------
    # TOTAL
    # --------------------------------------------------------

    total_point = None
    over_price = None
    under_price = None

    if total:

        total_point = total[
            "point"
        ]

        over_price = total[
            "over_price"
        ]

        under_price = total[
            "under_price"
        ]


    # --------------------------------------------------------
    # NO-VIG MONEYLINE
    # --------------------------------------------------------

    away_no_vig = None
    home_no_vig = None
    hold = None

    if (
        away_ml is not None
        and home_ml is not None
    ):

        (
            away_no_vig,
            home_no_vig,
            hold
        ) = calculate_no_vig(
            away_ml,
            home_ml
        )


    return {

        "key":
            book.get("key", ""),

        "name":
            book.get("title")
            or book.get("key")
            or "Unknown",

        "last_update":
            book.get("last_update"),

        "link":
            book.get("link"),

        "app_link":
            book.get("app_link"),


        # Raw moneyline
        "away_ml":
            away_ml,

        "home_ml":
            home_ml,


        # Formatted moneyline
        "away_ml_display":
            format_american(
                away_ml
            ),

        "home_ml_display":
            format_american(
                home_ml
            ),


        # Spreads
        "away_spread":
            away_spread,

        "away_spread_price":
            away_spread_price,

        "home_spread":
            home_spread,

        "home_spread_price":
            home_spread_price,


        "away_spread_display":
            (
                f"{format_spread(away_spread)} "
                f"{format_american(away_spread_price)}"
                if away_spread is not None
                else "—"
            ),

        "home_spread_display":
            (
                f"{format_spread(home_spread)} "
                f"{format_american(home_spread_price)}"
                if home_spread is not None
                else "—"
            ),


        # Totals
        "total":
            total_point,

        "over_price":
            over_price,

        "under_price":
            under_price,

        "over_display":
            (
                f"O {format_point(total_point)} "
                f"{format_american(over_price)}"
                if total_point is not None
                else "—"
            ),

        "under_display":
            (
                f"U {format_point(total_point)} "
                f"{format_american(under_price)}"
                if total_point is not None
                else "—"
            ),


        # Probability
        "away_no_vig":
            away_no_vig,

        "home_no_vig":
            home_no_vig,

        "hold":
            hold,

        "away_no_vig_display":
            probability_display(
                away_no_vig
            ),

        "home_no_vig_display":
            probability_display(
                home_no_vig
            ),

        "hold_display":
            (
                f"{hold * 100:.2f}%"
                if hold is not None
                else "—"
            ),


        # Best-line flags
        "best_away_ml": False,
        "best_home_ml": False,

        "best_away_spread": False,
        "best_home_spread": False,

        "best_over": False,
        "best_under": False
    }


# ============================================================
# BEST PRICE LOGIC
# ============================================================

def best_american(values):
    """
    Higher American odds are always better for the bettor.

    +120 beats +110
    -105 beats -110
    """

    valid = [
        value
        for value in values
        if value is not None
    ]

    if not valid:
        return None

    return max(valid)


def best_spread(
    books,
    side,
    consensus_spread
):
    """
    Find the best PRICE at the consensus spread.

    This prevents alternate lines such as +20.5 from
    competing with the market's primary +2.5 spread.
    """

    if consensus_spread is None:
        return None

    if side == "away":
        target_point = consensus_spread
    else:
        target_point = -consensus_spread

    candidates = []

    for book in books:

        point = book.get(
            f"{side}_spread"
        )

        price = book.get(
            f"{side}_spread_price"
        )

        if (
            point is None
            or price is None
        ):
            continue

        if abs(
            point - target_point
        ) > 0.01:
            continue

        candidates.append(
            (
                point,
                price
            )
        )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: item[1]
    )


def best_total(
    books,
    side,
    consensus_total
):
    """
    Find the best PRICE at the consensus total.

    Example:

        Consensus total = 50.5

    Compare:
        O50.5 -105
        O50.5 -110
        O50.5 +100

    Do NOT compare alternate totals such as O26.5.
    """

    if consensus_total is None:
        return None

    candidates = []

    for book in books:

        point = book.get("total")

        price = book.get(
            f"{side}_price"
        )

        if (
            point is None
            or price is None
        ):
            continue

        if abs(
            point - consensus_total
        ) > 0.01:
            continue

        candidates.append(
            (
                point,
                price
            )
        )

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: item[1]
    )


def mark_best_prices(books, consensus):
    """
    Add flags used by the UI to highlight the best available
    price/line in green.
    """

    best_away_ml = best_american(
        [
            book["away_ml"]
            for book in books
        ]
    )

    best_home_ml = best_american(
        [
            book["home_ml"]
            for book in books
        ]
    )


    best_away_spread = best_spread(
    books,
    "away",
    consensus.get("spread")
)

    best_home_spread = best_spread(
        books,
        "home",
        consensus.get("spread")
    )


    best_over = best_total(
    books,
    "over",
    consensus.get("total")
)

    best_under = best_total(
    books,
    "under",
    consensus.get("total")
)


    for book in books:

        if (
            best_away_ml is not None
            and book["away_ml"]
            == best_away_ml
        ):
            book["best_away_ml"] = True


        if (
            best_home_ml is not None
            and book["home_ml"]
            == best_home_ml
        ):
            book["best_home_ml"] = True


        if best_away_spread:

            if (
                book["away_spread"]
                == best_away_spread[0]
                and
                book["away_spread_price"]
                == best_away_spread[1]
            ):
                book[
                    "best_away_spread"
                ] = True


        if best_home_spread:

            if (
                book["home_spread"]
                == best_home_spread[0]
                and
                book["home_spread_price"]
                == best_home_spread[1]
            ):
                book[
                    "best_home_spread"
                ] = True


        if best_over:

            if (
                book["total"]
                == best_over[0]
                and
                book["over_price"]
                == best_over[1]
            ):
                book[
                    "best_over"
                ] = True


        if best_under:

            if (
                book["total"]
                == best_under[0]
                and
                book["under_price"]
                == best_under[1]
            ):
                book[
                    "best_under"
                ] = True


# ============================================================
# CONSENSUS CALCULATIONS
# ============================================================

def median(values):
    """
    Basic median helper.
    """

    values = sorted(
        [
            value
            for value in values
            if value is not None
        ]
    )

    if not values:
        return None

    n = len(values)

    middle = n // 2

    if n % 2:
        return values[middle]

    return (
        values[middle - 1]
        +
        values[middle]
    ) / 2


def calculate_consensus(
    books,
    away_team,
    home_team
):
    """
    Build market-wide summary statistics.
    """

    away_probabilities = []

    home_probabilities = []

    holds = []


    for book in books:

        if (
            book["away_no_vig"]
            is not None
        ):
            away_probabilities.append(
                book["away_no_vig"]
            )

        if (
            book["home_no_vig"]
            is not None
        ):
            home_probabilities.append(
                book["home_no_vig"]
            )

        if book["hold"] is not None:
            holds.append(
                book["hold"]
            )


    away_consensus = (
        sum(away_probabilities)
        / len(away_probabilities)
        if away_probabilities
        else None
    )

    home_consensus = (
        sum(home_probabilities)
        / len(home_probabilities)
        if home_probabilities
        else None
    )


    spread_values = [
        book["away_spread"]
        for book in books
        if book["away_spread"]
        is not None
    ]

    total_values = [
        book["total"]
        for book in books
        if book["total"]
        is not None
    ]


    consensus_spread = median(
        spread_values
    )

    consensus_total = median(
        total_values
    )


    average_hold = (
        sum(holds) / len(holds)
        if holds
        else None
    )


    return {

        "away_probability":
            away_consensus,

        "home_probability":
            home_consensus,

        "away_probability_display":
            probability_display(
                away_consensus
            ),

        "home_probability_display":
            probability_display(
                home_consensus
            ),

        "spread":
            consensus_spread,

        "spread_display":
            (
                format_spread(
                    consensus_spread
                )
                if consensus_spread
                is not None
                else "—"
            ),

        "total":
            consensus_total,

        "total_display":
            (
                format_point(
                    consensus_total
                )
                if consensus_total
                is not None
                else "—"
            ),

        "average_hold":
            average_hold,

        "average_hold_display":
            (
                f"{average_hold * 100:.2f}%"
                if average_hold is not None
                else "—"
            )
    }


# ============================================================
# BEST AVAILABLE SUMMARY
# ============================================================

def build_best_summary(
    books,
    away_team,
    home_team
):

    away_ml_books = [
        book
        for book in books
        if book["away_ml"]
        is not None
    ]

    home_ml_books = [
        book
        for book in books
        if book["home_ml"]
        is not None
    ]


    best_away_ml_book = (
        max(
            away_ml_books,
            key=lambda x: x[
                "away_ml"
            ]
        )
        if away_ml_books
        else None
    )


    best_home_ml_book = (
        max(
            home_ml_books,
            key=lambda x: x[
                "home_ml"
            ]
        )
        if home_ml_books
        else None
    )


    best_away_spread_book = None
    best_home_spread_book = None

    best_over_book = None
    best_under_book = None


    for book in books:

        if book["best_away_spread"]:
            best_away_spread_book = book

        if book["best_home_spread"]:
            best_home_spread_book = book

        if book["best_over"]:
            best_over_book = book

        if book["best_under"]:
            best_under_book = book


    return {

        "away_ml":
            (
                best_away_ml_book[
                    "away_ml_display"
                ]
                if best_away_ml_book
                else "—"
            ),

        "away_ml_book":
            (
                best_away_ml_book[
                    "name"
                ]
                if best_away_ml_book
                else ""
            ),


        "home_ml":
            (
                best_home_ml_book[
                    "home_ml_display"
                ]
                if best_home_ml_book
                else "—"
            ),

        "home_ml_book":
            (
                best_home_ml_book[
                    "name"
                ]
                if best_home_ml_book
                else ""
            ),


        "away_spread":
            (
                best_away_spread_book[
                    "away_spread_display"
                ]
                if best_away_spread_book
                else "—"
            ),

        "away_spread_book":
            (
                best_away_spread_book[
                    "name"
                ]
                if best_away_spread_book
                else ""
            ),


        "home_spread":
            (
                best_home_spread_book[
                    "home_spread_display"
                ]
                if best_home_spread_book
                else "—"
            ),

        "home_spread_book":
            (
                best_home_spread_book[
                    "name"
                ]
                if best_home_spread_book
                else ""
            ),


        "over":
            (
                best_over_book[
                    "over_display"
                ]
                if best_over_book
                else "—"
            ),

        "over_book":
            (
                best_over_book[
                    "name"
                ]
                if best_over_book
                else ""
            ),


        "under":
            (
                best_under_book[
                    "under_display"
                ]
                if best_under_book
                else "—"
            ),

        "under_book":
            (
                best_under_book[
                    "name"
                ]
                if best_under_book
                else ""
            )
    }


# ============================================================
# PARSE GAME
# ============================================================

def parse_game(event):

    away_team = event.get(
        "away_team",
        "Away"
    )

    home_team = event.get(
        "home_team",
        "Home"
    )


    books = []


    for raw_book in event.get(
        "bookmakers",
        []
    ):

        try:

            parsed = parse_book(
                raw_book,
                away_team,
                home_team
            )


            # Only show books with at least one usable market.
            if any([

                parsed["away_ml"]
                is not None,

                parsed["home_ml"]
                is not None,

                parsed["away_spread"]
                is not None,

                parsed["home_spread"]
                is not None,

                parsed["total"]
                is not None

            ]):

                books.append(
                    parsed
                )

        except Exception as error:

            print(
                "BOOK PARSE ERROR:",
                raw_book.get(
                    "title"
                ),
                repr(error)
            )


    books.sort(
        key=lambda x:
            x["name"].lower()
    )

    consensus = calculate_consensus(
    books,
    away_team,
    home_team
)

    mark_best_prices(
    books,
    consensus
)


    


    best = build_best_summary(
        books,
        away_team,
        home_team
    )


    date_info = format_datetime(
        event.get(
            "commence_time"
        )
    )


    return {

        "id":
            event.get("id"),

        "away":
            away_team,

        "home":
            home_team,

        "commence_time":
            event.get(
                "commence_time"
            ),

        "date":
            date_info["date"],

        "time":
            date_info["time"],

        "iso_time":
            date_info["iso"],

        "live":
            bool(
                event.get("live")
            ),

        "last_update":
            event.get(
                "last_update"
            ),

        "books":
            books,

        "book_count":
            len(books),

        "consensus":
            consensus,

        "best":
            best
    }


# ============================================================
# FETCH DATA
# ============================================================

def fetch_games(force=False):
    """
    Fetch NCAAF odds from PropLine.

    Uses an in-memory cache so browser refreshes don't
    unnecessarily consume API requests.
    """

    now = time.time()


    if (
        not force
        and CACHE["games"]
        and now - CACHE["timestamp"]
        < CACHE_SECONDS
    ):

        return (
            CACHE["games"],
            CACHE["meta"]
        )


    if not API_KEY:

        raise RuntimeError(
            "PROPLINE_API_KEY was not found."
        )


    print()
    print("==============================")
    print("FETCHING PROPLINE NCAAF ODDS")
    print("==============================")


    response = requests.get(

        PROPLINE_URL,

        params={
            "apiKey":
                API_KEY,

            "markets":
                MARKETS
        },

        timeout=
            REQUEST_TIMEOUT
    )


    print(
        "STATUS:",
        response.status_code
    )


    response.raise_for_status()


    raw = response.json()


    if isinstance(raw, list):

        events = raw

    else:

        events = raw.get(
            "events",
            []
        )


    print(
        "RAW EVENTS:",
        len(events)
    )


    games = []


    for event in events:

        try:

            game = parse_game(
                event
            )

            games.append(
                game
            )

        except Exception as error:

            print(
                "GAME PARSE ERROR:",
                event.get("id"),
                repr(error)
            )


    # Sort upcoming games chronologically.
    games.sort(
        key=lambda game:
            game.get(
                "commence_time"
            ) or ""
    )


    all_books = sorted(
        {
            book["name"]
            for game in games
            for book in game["books"]
        }
    )


    games_with_odds = sum(
        1
        for game in games
        if game["book_count"] > 0
    )


    multi_book_games = sum(
        1
        for game in games
        if game["book_count"] >= 2
    )


    meta = {

        "game_count":
            len(games),

        "games_with_odds":
            games_with_odds,

        "multi_book_games":
            multi_book_games,

        "sportsbooks":
            all_books,

        "sportsbook_count":
            len(all_books),

        "fetched_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "daily_limit":
            response.headers.get(
                "X-Daily-Limit"
            ),

        "daily_used":
            response.headers.get(
                "X-Daily-Used"
            ),

        "daily_remaining":
            response.headers.get(
                "X-Daily-Remaining"
            )
    }

    rows_saved = save_odds_snapshot(games)

    print("HISTORICAL ODDS SAVED:",
    rows_saved)

    database_stats = get_database_stats()

    print("DATABASE:",
    database_stats)

    CACHE["timestamp"] = now
    CACHE["games"] = games
    CACHE["meta"] = meta

    


    print(
        "PARSED GAMES:",
        len(games)
    )

    print(
        "SPORTSBOOKS FOUND:",
        len(all_books)
    )

    print(
        "BOOKS:",
        ", ".join(all_books)
    )

    print("==============================")
    print()


    return games, meta


# ============================================================
# HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>OddsScope | NCAA Football</title>


<style>

    /* ======================================================
       RESET
       ====================================================== */

    * {
        box-sizing: border-box;
    }

    html {
        scroll-behavior: smooth;
    }

    body {
        margin: 0;

        background:
            radial-gradient(
                circle at 15% 0%,
                rgba(45, 212, 191, 0.08),
                transparent 25%
            ),
            radial-gradient(
                circle at 90% 5%,
                rgba(59, 130, 246, 0.07),
                transparent 22%
            ),
            #070b12;

        color: #f8fafc;

        font-family:
            Inter,
            ui-sans-serif,
            system-ui,
            -apple-system,
            BlinkMacSystemFont,
            "Segoe UI",
            sans-serif;

        min-height: 100vh;
    }


    button,
    input,
    select {
        font: inherit;
    }


    /* ======================================================
       COLORS
       ====================================================== */

    :root {

        --background:
            #070b12;

        --panel:
            #0d131d;

        --panel-light:
            #111925;

        --panel-hover:
            #141e2b;

        --border:
            #1f2a38;

        --border-light:
            #293548;

        --text:
            #f8fafc;

        --muted:
            #8d9bad;

        --green:
            #34d399;

        --green-dark:
            #0d3b31;

        --green-soft:
            rgba(52, 211, 153, 0.10);

        --blue:
            #60a5fa;

        --yellow:
            #fbbf24;

        --red:
            #fb7185;
    }


    /* ======================================================
       HEADER
       ====================================================== */

    .topbar {

        position: sticky;
        top: 0;
        z-index: 100;

        backdrop-filter:
            blur(18px);

        background:
            rgba(7, 11, 18, 0.88);

        border-bottom:
            1px solid var(--border);
    }


    .topbar-inner {

        max-width:
            1500px;

        margin:
            0 auto;

        padding:
            18px 28px;

        display:
            flex;

        align-items:
            center;

        justify-content:
            space-between;

        gap:
            25px;
    }


    .brand {

        display:
            flex;

        align-items:
            center;

        gap:
            12px;

        min-width:
            210px;
    }


    .brand-icon {

        width:
            38px;

        height:
            38px;

        border-radius:
            11px;

        display:
            flex;

        align-items:
            center;

        justify-content:
            center;

        background:
            linear-gradient(
                135deg,
                #34d399,
                #22c55e
            );

        color:
            #04110d;

        font-weight:
            900;

        font-size:
            20px;

        box-shadow:
            0 0 30px
            rgba(52, 211, 153, 0.18);
    }


    .brand-text {

        font-size:
            21px;

        font-weight:
            850;

        letter-spacing:
            -0.6px;
    }


    .brand-text span {
        color:
            var(--green);
    }


    .header-status {

        display:
            flex;

        align-items:
            center;

        gap:
            8px;

        color:
            var(--muted);

        font-size:
            13px;
    }


    .live-dot {

        width:
            8px;

        height:
            8px;

        border-radius:
            50%;

        background:
            var(--green);

        box-shadow:
            0 0 10px
            var(--green);
    }


    /* ======================================================
       PAGE
       ====================================================== */

    .page {

        max-width:
            1500px;

        margin:
            0 auto;

        padding:
            42px 28px 100px;
    }


    /* ======================================================
       HERO
       ====================================================== */

    .hero {

        margin-bottom:
            30px;
    }


    .eyebrow {

        color:
            var(--green);

        font-size:
            12px;

        font-weight:
            800;

        letter-spacing:
            1.6px;

        text-transform:
            uppercase;

        margin-bottom:
            10px;
    }


    h1 {

        margin:
            0;

        font-size:
            clamp(
                32px,
                5vw,
                54px
            );

        letter-spacing:
            -2px;

        line-height:
            1.02;
    }


    .hero-description {

        color:
            var(--muted);

        font-size:
            16px;

        line-height:
            1.7;

        max-width:
            760px;

        margin-top:
            14px;
    }


    /* ======================================================
       STAT CARDS
       ====================================================== */

    .stats {

        display:
            grid;

        grid-template-columns:
            repeat(
                4,
                minmax(0, 1fr)
            );

        gap:
            12px;

        margin:
            28px 0;
    }


    .stat-card {

        background:
            linear-gradient(
                180deg,
                rgba(255,255,255,0.025),
                rgba(255,255,255,0.005)
            ),
            var(--panel);

        border:
            1px solid var(--border);

        border-radius:
            14px;

        padding:
            17px 18px;
    }


    .stat-label {

        color:
            var(--muted);

        font-size:
            11px;

        font-weight:
            750;

        text-transform:
            uppercase;

        letter-spacing:
            1px;

        margin-bottom:
            6px;
    }


    .stat-value {

        font-size:
            25px;

        font-weight:
            850;

        letter-spacing:
            -0.7px;
    }


    .stat-value.green {
        color:
            var(--green);
    }


    /* ======================================================
       CONTROLS
       ====================================================== */

    .controls {

        display:
            flex;

        align-items:
            center;

        justify-content:
            space-between;

        gap:
            14px;

        flex-wrap:
            wrap;

        margin:
            26px 0;
    }


    .search {

        flex:
            1;

        min-width:
            260px;

        position:
            relative;
    }


    .search input {

        width:
            100%;

        height:
            46px;

        padding:
            0 16px 0 43px;

        border-radius:
            11px;

        border:
            1px solid var(--border);

        background:
            var(--panel);

        color:
            var(--text);

        outline:
            none;

        transition:
            0.2s ease;
    }


    .search input:focus {

        border-color:
            rgba(52, 211, 153, 0.55);

        box-shadow:
            0 0 0 3px
            rgba(52, 211, 153, 0.08);
    }


    .search-icon {

        position:
            absolute;

        left:
            15px;

        top:
            50%;

        transform:
            translateY(-50%);

        color:
            var(--muted);
    }


    .filters {

        display:
            flex;

        gap:
            7px;

        flex-wrap:
            wrap;
    }


    .filter-button {

        border:
            1px solid var(--border);

        background:
            var(--panel);

        color:
            var(--muted);

        padding:
            10px 14px;

        border-radius:
            9px;

        cursor:
            pointer;

        font-size:
            13px;

        font-weight:
            700;

        transition:
            0.15s ease;
    }


    .filter-button:hover {

        border-color:
            var(--border-light);

        color:
            white;
    }


    .filter-button.active {

        color:
            #05110d;

        background:
            var(--green);

        border-color:
            var(--green);
    }


    /* ======================================================
       SPORTSBOOK CHIPS
       ====================================================== */

    .sportsbooks-strip {

        display:
            flex;

        align-items:
            center;

        gap:
            8px;

        overflow-x:
            auto;

        padding:
            0 0 18px;

        scrollbar-width:
            thin;
    }


    .book-chip {

        white-space:
            nowrap;

        padding:
            7px 10px;

        border:
            1px solid var(--border);

        border-radius:
            999px;

        background:
            var(--panel);

        color:
            var(--muted);

        font-size:
            11px;

        font-weight:
            700;
    }


    /* ======================================================
       DATE DIVIDER
       ====================================================== */

    .date-divider {

        margin:
            36px 0 14px;

        display:
            flex;

        align-items:
            center;

        gap:
            13px;

        color:
            #b8c4d2;

        font-size:
            12px;

        font-weight:
            800;

        text-transform:
            uppercase;

        letter-spacing:
            1.1px;
    }


    .date-divider::after {

        content:
            "";

        height:
            1px;

        flex:
            1;

        background:
            var(--border);
    }


    /* ======================================================
       GAME CARD
       ====================================================== */

    .game-card {

        background:
            linear-gradient(
                180deg,
                rgba(255,255,255,0.018),
                transparent 45%
            ),
            var(--panel);

        border:
            1px solid var(--border);

        border-radius:
            17px;

        overflow:
            hidden;

        margin-bottom:
            17px;

        box-shadow:
            0 12px 40px
            rgba(0,0,0,0.12);

        transition:
            border-color 0.2s ease,
            transform 0.2s ease;
    }


    .game-card:hover {

        border-color:
            #2b394a;
    }


    .game-header {

        padding:
            21px 22px 18px;

        display:
            flex;

        justify-content:
            space-between;

        align-items:
            flex-start;

        gap:
            20px;
    }


    .game-time {

        color:
            var(--muted);

        font-size:
            11px;

        font-weight:
            700;

        text-transform:
            uppercase;

        letter-spacing:
            0.7px;

        margin-bottom:
            8px;
    }


    .matchup {

        font-size:
            22px;

        font-weight:
            820;

        letter-spacing:
            -0.6px;
    }


    .at {

        color:
            #59687a;

        font-weight:
            500;

        margin:
            0 7px;
    }


    .book-count {

        color:
            var(--green);

        background:
            var(--green-soft);

        border:
            1px solid
            rgba(52, 211, 153, 0.18);

        border-radius:
            999px;

        padding:
            7px 10px;

        font-size:
            11px;

        font-weight:
            800;

        white-space:
            nowrap;
    }


    /* ======================================================
       BEST AVAILABLE
       ====================================================== */

    .best-strip {

        border-top:
            1px solid var(--border);

        border-bottom:
            1px solid var(--border);

        background:
            rgba(52, 211, 153, 0.025);

        padding:
            15px 22px;
    }


    .best-label {

        color:
            var(--green);

        font-size:
            10px;

        font-weight:
            850;

        letter-spacing:
            1.3px;

        text-transform:
            uppercase;

        margin-bottom:
            11px;
    }


    .best-grid {

        display:
            grid;

        grid-template-columns:
            repeat(
                6,
                minmax(0, 1fr)
            );

        gap:
            10px;
    }


    .best-item {

        min-width:
            0;
    }


    .best-market {

        color:
            var(--muted);

        font-size:
            9px;

        text-transform:
            uppercase;

        letter-spacing:
            0.7px;

        margin-bottom:
            3px;
    }


    .best-value {

        color:
            #eafff6;

        font-size:
            14px;

        font-weight:
            800;
    }


    .best-book {

        color:
            #6f7f91;

        font-size:
            9px;

        overflow:
            hidden;

        text-overflow:
            ellipsis;

        white-space:
            nowrap;

        margin-top:
            2px;
    }


    /* ======================================================
       CONSENSUS
       ====================================================== */

    .consensus {

        display:
            grid;

        grid-template-columns:
            repeat(
                4,
                minmax(0, 1fr)
            );

        border-bottom:
            1px solid var(--border);
    }


    .consensus-item {

        padding:
            13px 22px;

        border-right:
            1px solid var(--border);
    }


    .consensus-item:last-child {
        border-right:
            none;
    }


    .consensus-label {

        color:
            var(--muted);

        font-size:
            9px;

        text-transform:
            uppercase;

        letter-spacing:
            0.7px;
    }


    .consensus-value {

        margin-top:
            3px;

        font-size:
            13px;

        font-weight:
            750;
    }


    /* ======================================================
       TABLE
       ====================================================== */

    .table-wrapper {

        overflow-x:
            auto;
    }


    table {

        width:
            100%;

        border-collapse:
            collapse;

        min-width:
            950px;
    }


    th {

        color:
            #748397;

        font-size:
            9px;

        font-weight:
            800;

        text-transform:
            uppercase;

        letter-spacing:
            0.75px;

        text-align:
            right;

        padding:
            12px 13px;

        border-bottom:
            1px solid var(--border);

        background:
            rgba(255,255,255,0.008);
    }


    th:first-child {

        text-align:
            left;

        padding-left:
            22px;
    }


    td {

        text-align:
            right;

        padding:
            12px 13px;

        border-bottom:
            1px solid
            rgba(31, 42, 56, 0.68);

        font-size:
            12px;

        font-variant-numeric:
            tabular-nums;
    }


    td:first-child {

        text-align:
            left;

        padding-left:
            22px;
    }


    tr:last-child td {
        border-bottom:
            none;
    }


    tbody tr {

        transition:
            background 0.12s ease;
    }


    tbody tr:hover {

        background:
            rgba(255,255,255,0.018);
    }


    .book-name {

        font-weight:
            750;

        color:
            #dce5ef;
    }


    .odds-cell {

        color:
            #b9c6d5;

        font-weight:
            650;

        border-radius:
            6px;
    }


    .best-cell {

        color:
            var(--green);

        font-weight:
            850;

        background:
            rgba(52, 211, 153, 0.055);
    }


    .best-cell::after {

        content:
            " BEST";

        font-size:
            7px;

        vertical-align:
            top;

        margin-left:
            3px;

        color:
            #4ade80;
    }


    /* ======================================================
       EXPANDABLE ANALYSIS
       ====================================================== */

    details {

        border-top:
            1px solid var(--border);
    }


    summary {

        cursor:
            pointer;

        padding:
            14px 22px;

        color:
            var(--muted);

        font-size:
            11px;

        font-weight:
            750;

        user-select:
            none;

        transition:
            color 0.15s ease;
    }


    summary:hover {
        color:
            white;
    }


    .analysis-content {

        padding:
            0 22px 20px;
    }


    .analysis-note {

        color:
            #718196;

        font-size:
            11px;

        line-height:
            1.6;

        margin-bottom:
            12px;
    }


    .prob-table {

        width:
            100%;

        min-width:
            0;
    }


    .prob-table th,
    .prob-table td {

        padding:
            8px 10px;

        font-size:
            10px;
    }


    /* ======================================================
       EMPTY
       ====================================================== */

    .empty {

        text-align:
            center;

        padding:
            80px 20px;

        color:
            var(--muted);
    }


    /* ======================================================
       FOOTER
       ====================================================== */

    .footer {

        margin-top:
            55px;

        padding-top:
            22px;

        border-top:
            1px solid var(--border);

        color:
            #59687a;

        font-size:
            10px;

        line-height:
            1.7;
    }


    /* ======================================================
       RESPONSIVE
       ====================================================== */

    @media (
        max-width: 900px
    ) {

        .stats {

            grid-template-columns:
                repeat(
                    2,
                    minmax(0, 1fr)
                );
        }


        .best-grid {

            grid-template-columns:
                repeat(
                    3,
                    minmax(0, 1fr)
                );
        }


        .consensus {

            grid-template-columns:
                repeat(
                    2,
                    minmax(0, 1fr)
                );
        }


        .consensus-item:nth-child(2) {

            border-right:
                none;
        }


        .consensus-item:nth-child(-n+2) {

            border-bottom:
                1px solid var(--border);
        }

    }


    @media (
        max-width: 600px
    ) {

        .topbar-inner,
        .page {

            padding-left:
                16px;

            padding-right:
                16px;
        }


        .header-status {
            display:
                none;
        }


        .stats {

            grid-template-columns:
                repeat(
                    2,
                    minmax(0, 1fr)
                );
        }


        .game-header {

            padding:
                17px;
        }


        .matchup {

            font-size:
                18px;
        }


        .best-strip {

            padding:
                14px 17px;
        }


        .best-grid {

            grid-template-columns:
                repeat(
                    2,
                    minmax(0, 1fr)
                );
        }
    }

    /* ======================================================
       MARKET INTELLIGENCE
       ====================================================== */

    .intelligence {
        margin: 42px 0 36px;
    }

    .intel-header {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 20px;
        margin-bottom: 18px;
    }

    .intel-title {
        margin: 2px 0 0;
        font-size: 30px;
        letter-spacing: -1px;
    }

    .intel-subtitle {
        color: var(--muted);
        font-size: 11px;
        margin-top: 6px;
        line-height: 1.5;
    }

    .intel-live {
        display: flex;
        align-items: center;
        gap: 7px;
        color: var(--green);
        font-size: 9px;
        font-weight: 900;
        letter-spacing: 1px;
        padding: 7px 10px;
        border: 1px solid rgba(52, 211, 153, 0.18);
        border-radius: 999px;
        background: rgba(52, 211, 153, 0.05);
        white-space: nowrap;
    }

    .intel-live-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--green);
        box-shadow: 0 0 8px var(--green);
    }

    .intel-stats {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        border: 1px solid var(--border);
        border-radius: 13px;
        background: var(--panel);
        overflow: hidden;
        margin-bottom: 26px;
    }

    .intel-stat {
        padding: 14px 17px;
        border-right: 1px solid var(--border);
    }

    .intel-stat:last-child {
        border-right: none;
    }

    .intel-stat-label {
        display: block;
        color: var(--muted);
        font-size: 8px;
        font-weight: 800;
        letter-spacing: 0.8px;
        text-transform: uppercase;
        margin-bottom: 5px;
    }

    .intel-stat strong {
        font-size: 20px;
        font-variant-numeric: tabular-nums;
    }

    .intel-green {
        color: var(--green);
    }

    .intel-yellow {
        color: var(--yellow);
    }

    .signal-section-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 20px;
        margin-bottom: 10px;
    }

    .signal-section-title {
        font-size: 11px;
        font-weight: 850;
        text-transform: uppercase;
        letter-spacing: 1px;
    }

    .signal-section-count {
        color: var(--muted);
        font-size: 9px;
        margin-left: 8px;
    }

    .signal-legend {
        display: flex;
        align-items: center;
        gap: 14px;
        color: var(--muted);
        font-size: 8px;
    }

    .signal-legend span {
        display: flex;
        align-items: center;
        gap: 5px;
    }

    .legend-dot {
        display: inline-block;
        width: 6px;
        height: 6px;
        border-radius: 50%;
    }

    .strong-dot { background: var(--green); }
    .medium-dot { background: var(--yellow); }
    .small-dot { background: #64748b; }

    .signal-list {
        border: 1px solid var(--border);
        border-radius: 14px;
        overflow: hidden;
        background: var(--panel);
    }

    .signal-row {
        position: relative;
        display: grid;
        grid-template-columns:
            46px
            minmax(260px, 1.7fr)
            minmax(110px, 0.8fr)
            95px
            95px
            60px
            72px;
        align-items: center;
        min-height: 88px;
        border-bottom: 1px solid var(--border);
        transition: background 0.15s ease;
    }

    .signal-row:last-child {
        border-bottom: none;
    }

    .signal-row:hover {
        background: var(--panel-hover);
    }

    .signal-row::before {
        content: "";
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        width: 2px;
        background: #475569;
    }

    .signal-row-strong::before { background: var(--green); }
    .signal-row-medium::before { background: var(--yellow); }

    .signal-rank {
        color: #475569;
        font-size: 10px;
        font-weight: 850;
        text-align: center;
        font-variant-numeric: tabular-nums;
    }

    .signal-main {
        padding: 14px 12px;
        min-width: 0;
    }

    .signal-row-top {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 5px;
        min-width: 0;
    }

    .signal-matchup {
        color: #8190a2;
        font-size: 9px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .signal-at {
        color: #475569;
        margin: 0 2px;
    }

    .signal-market-type {
        color: #637286;
        font-size: 8px;
        font-weight: 850;
        letter-spacing: 0.8px;
        text-transform: uppercase;
        margin-bottom: 3px;
    }

    .signal-selection {
        color: #f8fafc;
        font-size: 16px;
        font-weight: 850;
        letter-spacing: -0.3px;
    }

    .signal-strength {
        flex: 0 0 auto;
        padding: 3px 6px;
        border-radius: 999px;
        font-size: 7px;
        font-weight: 900;
        text-transform: uppercase;
        letter-spacing: 0.6px;
    }

    .signal-strength-strong {
        color: var(--green);
        background: rgba(52, 211, 153, 0.08);
    }

    .signal-strength-medium {
        color: var(--yellow);
        background: rgba(251, 191, 36, 0.08);
    }

    .signal-strength-small {
        color: #94a3b8;
        background: rgba(148, 163, 184, 0.07);
    }

    .signal-offer-column,
    .signal-price-column,
    .signal-edge-column,
    .signal-books-column {
        padding: 13px 9px;
        min-width: 0;
    }

    .signal-column-label {
        display: block;
        color: #5f6d7e;
        font-size: 7px;
        font-weight: 750;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        margin-bottom: 5px;
    }

    .signal-sportsbook {
        display: block;
        color: #cbd5e1;
        font-size: 10px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .signal-price {
        font-size: 13px;
        font-variant-numeric: tabular-nums;
    }

    .signal-edge {
        color: var(--green);
        font-size: 13px;
        font-variant-numeric: tabular-nums;
    }

    .signal-books-column strong {
        font-size: 12px;
    }

    .signal-expand {
        text-align: center;
    }

    .signal-details-button {
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.015);
        color: var(--muted);
        padding: 6px 7px;
        border-radius: 7px;
        font-size: 8px;
        font-weight: 750;
        cursor: pointer;
    }

    .signal-details-button:hover {
        color: white;
        border-color: var(--border-light);
    }

    .signal-details {
        display: none;
        grid-column: 1 / -1;
        border-top: 1px solid var(--border);
        background: rgba(0,0,0,0.10);
    }

    .signal-row.expanded .signal-details {
        display: block;
    }

    .signal-details-inner {
        display: grid;
        grid-template-columns: minmax(300px, 1fr) 140px 140px;
        gap: 20px;
        padding: 14px 58px;
    }

    .signal-details-inner span {
        display: block;
        color: #5f6d7e;
        font-size: 8px;
        text-transform: uppercase;
        letter-spacing: 0.7px;
        margin-bottom: 4px;
    }

    .signal-details-inner p {
        color: #8493a6;
        font-size: 10px;
        line-height: 1.55;
        margin: 0;
        max-width: 700px;
    }

    .detail-metric strong {
        font-size: 12px;
    }

    .arb-price {
        display: block;
        color: var(--green);
        font-weight: 850;
        margin-top: 3px;
    }

    .no-signals {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 5px;
        padding: 32px;
        border: 1px dashed var(--border);
        border-radius: 13px;
        color: var(--muted);
        font-size: 10px;
    }

    @media (max-width: 1100px) {
        .signal-row {
            grid-template-columns:
                42px
                minmax(230px, 1.5fr)
                105px
                85px
                85px
                55px;
        }

        .signal-expand {
            display: none;
        }
    }

    @media (max-width: 760px) {
        .intel-header {
            align-items: flex-start;
        }

        .intel-stats {
            grid-template-columns: repeat(2, 1fr);
        }

        .intel-stat:nth-child(2) {
            border-right: none;
        }

        .intel-stat:nth-child(-n+2) {
            border-bottom: 1px solid var(--border);
        }

        .signal-legend {
            display: none;
        }

        .signal-row {
            grid-template-columns: 36px 1fr 82px 82px;
            padding: 8px 0;
        }

        .signal-offer-column,
        .signal-books-column,
        .signal-expand {
            display: none;
        }

        .signal-price-column,
        .signal-edge-column {
            padding: 8px;
        }
    }


/* PARLAY COMPARISON */
.parlay-lab{margin:34px 0;padding:20px;border:1px solid var(--border);border-radius:16px;background:var(--panel)}
.parlay-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-end;margin-bottom:16px}
.parlay-head h2{margin:2px 0 0;font-size:27px}.parlay-head p{margin:6px 0 0;color:var(--muted);font-size:10px;max-width:720px;line-height:1.5}
.parlay-badge{color:#93c5fd;border:1px solid rgba(96,165,250,.22);background:rgba(96,165,250,.06);padding:7px 10px;border-radius:999px;font-size:8px;font-weight:900;white-space:nowrap}
.parlay-grid{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}.parlay-box{border:1px solid var(--border);border-radius:12px;background:rgba(0,0,0,.10);overflow:hidden}
.parlay-box-head{padding:11px 13px;border-bottom:1px solid var(--border);font-size:9px;font-weight:850;text-transform:uppercase;letter-spacing:.7px}
.parlay-form{display:grid;grid-template-columns:1.3fr .7fr 1.2fr auto;gap:8px;padding:13px;align-items:end}
.parlay-field label{display:block;color:#64748b;font-size:7px;font-weight:850;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}
.parlay-field select,.parlay-field input{width:100%;min-height:38px;border:1px solid var(--border);border-radius:8px;background:#0b1018;color:var(--text);padding:0 9px;font-size:10px}
.parlay-btn{min-height:38px;border:1px solid var(--border-light);border-radius:8px;background:#111927;color:white;padding:0 12px;font-size:9px;font-weight:850;cursor:pointer}
.parlay-error{min-height:18px;padding:0 13px 8px;color:#fca5a5;font-size:8px}.parlay-slip,.parlay-results{padding:0 13px 13px}
.parlay-empty{padding:18px 8px;color:var(--muted);font-size:9px;text-align:center}.parlay-leg{display:grid;grid-template-columns:26px 1fr auto;gap:8px;align-items:center;padding:10px 0;border-top:1px solid var(--border)}
.parlay-num{display:grid;place-items:center;width:23px;height:23px;border-radius:7px;background:rgba(96,165,250,.08);color:#93c5fd;font-size:8px;font-weight:900}.parlay-game{color:#64748b;font-size:7px;text-transform:uppercase}.parlay-title{font-size:10px;font-weight:800;margin-top:2px}
.parlay-remove{border:0;background:transparent;color:#64748b;font-size:15px;cursor:pointer}.parlay-tools{display:flex;justify-content:space-between;gap:10px;align-items:end;padding:13px}
.parlay-table{width:100%;border-collapse:collapse;font-size:9px}.parlay-table th{color:#64748b;font-size:7px;text-transform:uppercase;text-align:left;padding:7px 5px;border-bottom:1px solid var(--border)}.parlay-table td{padding:9px 5px;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums}.parlay-table th:not(:first-child),.parlay-table td:not(:first-child){text-align:right}.parlay-best td{color:var(--green);font-weight:850}
.parlay-note{margin:10px 13px 13px;padding:9px;border:1px solid var(--border);border-radius:8px;color:#718096;font-size:8px;line-height:1.5}
@media(max-width:900px){.parlay-grid{grid-template-columns:1fr}.parlay-form{grid-template-columns:1fr 1fr}}@media(max-width:600px){.parlay-form{grid-template-columns:1fr}}


    /* MARKET MOVEMENT */
    .movement-lab{margin:34px 0;padding:20px;border:1px solid var(--border);border-radius:16px;background:var(--panel);overflow:hidden}
    .movement-head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:15px}
    .movement-head h2{margin:2px 0 0;font-size:27px;letter-spacing:-.7px}.movement-head p{margin:6px 0 0;color:var(--muted);font-size:10px;line-height:1.5;max-width:760px}
    .movement-live{display:flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid rgba(52,211,153,.2);border-radius:999px;color:var(--green);font-size:8px;font-weight:900;white-space:nowrap}
    .movement-dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:movementPulse 1.7s ease-in-out infinite}
    @keyframes movementPulse{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.25)}}
    .movement-controls{display:grid;grid-template-columns:minmax(240px,1.7fr) .7fr .55fr auto;gap:9px;align-items:end;margin-bottom:13px}
    .movement-field label{display:block;margin-bottom:5px;color:#64748b;font-size:7px;font-weight:850;text-transform:uppercase;letter-spacing:.7px}
    .movement-field select{width:100%;min-height:39px;border:1px solid var(--border);border-radius:8px;background:#0b1018;color:var(--text);padding:0 10px;font-size:10px}
    .movement-refresh{min-height:39px;border:1px solid var(--border-light);border-radius:8px;background:#111927;color:white;padding:0 13px;font-size:9px;font-weight:850;cursor:pointer}
    .movement-grid{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:13px}
    .movement-chart-card{position:relative;min-height:330px;border:1px solid var(--border);border-radius:12px;background:rgba(0,0,0,.11);padding:13px}
    .movement-chart-wrap{position:relative;height:300px}.movement-stats{display:grid;gap:9px}
    .movement-stat{border:1px solid var(--border);border-radius:11px;background:rgba(0,0,0,.11);padding:12px}
    .movement-stat-label{color:#64748b;font-size:7px;font-weight:850;text-transform:uppercase;letter-spacing:.7px;margin-bottom:5px}
    .movement-stat-value{font-size:18px;font-weight:900;font-variant-numeric:tabular-nums}.movement-stat-sub{margin-top:4px;color:var(--muted);font-size:8px;line-height:1.4}
    .movement-message{position:absolute;inset:0;display:grid;place-items:center;color:var(--muted);font-size:10px;text-align:center;padding:25px;z-index:2}
    .movement-spark{height:4px;margin-top:8px;border-radius:99px;background:linear-gradient(90deg,rgba(52,211,153,.18),rgba(96,165,250,.55),rgba(52,211,153,.18));background-size:200% 100%;animation:movementSweep 2.6s linear infinite}
    @keyframes movementSweep{to{background-position:-200% 0}}
    @media(prefers-reduced-motion:reduce){.movement-dot,.movement-spark{animation:none}}
    @media(max-width:900px){.movement-grid{grid-template-columns:1fr}.movement-stats{grid-template-columns:repeat(3,1fr)}.movement-controls{grid-template-columns:1fr 1fr}}
    @media(max-width:600px){.movement-head{align-items:flex-start}.movement-controls{grid-template-columns:1fr}.movement-stats{grid-template-columns:1fr}.movement-chart-wrap{height:270px}}

</style>

    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
</head>


<body>


<!-- ========================================================
     HEADER
     ======================================================== -->

<div class="topbar">

    <div class="topbar-inner">

        <div class="brand">

            <div class="brand-icon">
                O
            </div>

            <div class="brand-text">
                Odds<span>Scope</span>
            </div>

        </div>


        <div class="header-status">

            <span class="live-dot"></span>

            Live market comparison

        </div>

    </div>

</div>


<!-- ========================================================
     PAGE
     ======================================================== -->

<div class="page">


    <!-- HERO -->

    <section class="hero">

        <div class="eyebrow">
            NCAA Football
        </div>

        <h1>
            Find the best line.
        </h1>

        <div class="hero-description">

            Compare NCAA football moneylines,
            spreads and totals across sportsbooks.
            Best available prices are highlighted
            automatically.

        </div>

    </section>


    <!-- ====================================================
         STATS
         ==================================================== -->

    <div class="stats">

        <div class="stat-card">

            <div class="stat-label">
                Games
            </div>

            <div class="stat-value">
                {{ meta.game_count }}
            </div>

        </div>


        <div class="stat-card">

            <div class="stat-label">
                Games With Odds
            </div>

            <div class="stat-value">
                {{ meta.games_with_odds }}
            </div>

        </div>


        <div class="stat-card">

            <div class="stat-label">
                Multi-Book Games
            </div>

            <div class="stat-value green">
                {{ meta.multi_book_games }}
            </div>

        </div>


        <div class="stat-card">

            <div class="stat-label">
                Sportsbooks Found
            </div>

            <div class="stat-value green">
                {{ meta.sportsbook_count }}
            </div>

        </div>

    </div>


    <!-- ====================================================
         SPORTSBOOK LIST
         ==================================================== -->

    <div class="sportsbooks-strip">

        {% for book in meta.sportsbooks %}

            <div class="book-chip">
                {{ book }}
            </div>

        {% endfor %}

    </div>

    <!-- ====================================================
         MARKET INTELLIGENCE
         ==================================================== -->

    <section class="intelligence">

        <div class="intel-header">

            <div>
                <div class="eyebrow">
                    Market Intelligence
                </div>

                <h2 class="intel-title">
                    Market Signals
                </h2>

                <div class="intel-subtitle">
                    Price discrepancies detected across comparable sportsbook markets.
                </div>
            </div>

            <div class="intel-live">
                <span class="intel-live-dot"></span>
                LIVE MARKET
            </div>

        </div>


        {% set strong_signals =
            signals
            | selectattr("strength", "equalto", "strong")
            | list
        %}

        {% set medium_signals =
            signals
            | selectattr("strength", "equalto", "medium")
            | list
        %}

        {% set arb_signals =
            signals
            | selectattr("type", "equalto", "moneyline_arbitrage")
            | list
        %}


        <div class="intel-stats">

            <div class="intel-stat">
                <span class="intel-stat-label">
                    Signals
                </span>
                <strong>
                    {{ signals|length }}
                </strong>
            </div>

            <div class="intel-stat">
                <span class="intel-stat-label">
                    Strong
                </span>
                <strong class="intel-green">
                    {{ strong_signals|length }}
                </strong>
            </div>

            <div class="intel-stat">
                <span class="intel-stat-label">
                    Medium
                </span>
                <strong class="intel-yellow">
                    {{ medium_signals|length }}
                </strong>
            </div>

            <div class="intel-stat">
                <span class="intel-stat-label">
                    Arbitrage
                </span>
                <strong>
                    {{ arb_signals|length }}
                </strong>
            </div>

        </div>


        <div class="signal-section-header">

            <div>
                <span class="signal-section-title">
                    Top Market Signals
                </span>

                <span class="signal-section-count">
                    Showing up to 8 of {{ signals|length }}
                </span>
            </div>

            <div class="signal-legend">
                <span>
                    <i class="legend-dot strong-dot"></i>
                    Strong
                </span>
                <span>
                    <i class="legend-dot medium-dot"></i>
                    Medium
                </span>
                <span>
                    <i class="legend-dot small-dot"></i>
                    Small
                </span>
            </div>

        </div>


        {% if signals %}

        <div class="signal-list">

            {% for signal in signals[:8] %}

            <div class="
                signal-row
                signal-row-{{ signal.strength }}
            ">

                <div class="signal-rank">
                    {{ "%02d"|format(loop.index) }}
                </div>


                <div class="signal-main">

                    <div class="signal-row-top">

                        <span class="signal-matchup">
                            {{ signal.away }}
                            <span class="signal-at">@</span>
                            {{ signal.home }}
                        </span>

                        <span class="
                            signal-strength
                            signal-strength-{{ signal.strength }}
                        ">
                            {{ signal.strength }}
                        </span>

                    </div>

                    <div class="signal-market-type">

                        {% if signal.type == "moneyline_arbitrage" %}
                            ⚡ MONEYLINE ARBITRAGE
                        {% elif signal.type == "moneyline_value" %}
                            MONEYLINE · PRICE DISLOCATION
                        {% elif signal.type == "spread_price" %}
                            SPREAD · PRICE DISLOCATION
                        {% elif signal.type == "total_price" %}
                            TOTAL · PRICE DISLOCATION
                        {% else %}
                            MARKET SIGNAL
                        {% endif %}

                    </div>

                    <div class="signal-selection">
                        {{ signal.title }}
                    </div>

                </div>


                {% if signal.type != "moneyline_arbitrage" %}

                <div class="signal-offer-column">
                    <span class="signal-column-label">
                        Sportsbook
                    </span>
                    <strong class="signal-sportsbook">
                        {{ signal.sportsbook }}
                    </strong>
                </div>

                <div class="signal-price-column">
                    <span class="signal-column-label">
                        Price
                    </span>
                    <strong class="signal-price">
                        {% if signal.type == "spread_price" %}
                            {{ signal.line_display }}
                        {% endif %}
                        {{ signal.odds_display }}
                    </strong>
                </div>

                <div class="signal-edge-column">
                    <span class="signal-column-label">
                        Price Edge
                    </span>
                    <strong class="signal-edge">
                        {{ signal.edge_display }}
                    </strong>
                </div>

                <div class="signal-books-column">
                    <span class="signal-column-label">
                        Books
                    </span>
                    <strong>
                        {{ signal.book_count }}
                    </strong>
                </div>

                {% else %}

                <div class="signal-offer-column">
                    <span class="signal-column-label">
                        Away
                    </span>
                    <strong class="signal-sportsbook">
                        {{ signal.away_book }}
                    </strong>
                    <span class="arb-price">
                        {{ signal.away_odds_display }}
                    </span>
                </div>

                <div class="signal-offer-column">
                    <span class="signal-column-label">
                        Home
                    </span>
                    <strong class="signal-sportsbook">
                        {{ signal.home_book }}
                    </strong>
                    <span class="arb-price">
                        {{ signal.home_odds_display }}
                    </span>
                </div>

                <div class="signal-edge-column">
                    <span class="signal-column-label">
                        Arb Margin
                    </span>
                    <strong class="signal-edge">
                        {{ signal.edge_display }}
                    </strong>
                </div>

                <div class="signal-books-column">
                    <span class="signal-column-label">
                        Type
                    </span>
                    <strong>
                        ML
                    </strong>
                </div>

                {% endif %}


                <div class="signal-expand">
                    <button
                        class="signal-details-button"
                        type="button"
                        onclick="toggleSignalDetails(this)"
                    >
                        Details <span>⌄</span>
                    </button>
                </div>


                <div class="signal-details">

                    <div class="signal-details-inner">

                        <div>
                            <span>
                                Why it was flagged
                            </span>
                            <p>
                                {{ signal.description }}
                            </p>
                        </div>

                        {% if signal.market_probability_display is defined %}
                        <div class="detail-metric">
                            <span>
                                Market estimate
                            </span>
                            <strong>
                                {{ signal.market_probability_display }}
                            </strong>
                        </div>
                        {% endif %}

                        {% if signal.break_even_display is defined %}
                        <div class="detail-metric">
                            <span>
                                Break-even
                            </span>
                            <strong>
                                {{ signal.break_even_display }}
                            </strong>
                        </div>
                        {% elif signal.combined_probability_display is defined %}
                        <div class="detail-metric">
                            <span>
                                Combined implied
                            </span>
                            <strong>
                                {{ signal.combined_probability_display }}
                            </strong>
                        </div>
                        {% endif %}

                    </div>

                </div>

            </div>

            {% endfor %}

        </div>

        {% else %}

        <div class="no-signals">
            <strong>No notable signals right now.</strong>
            <span>
                OddsScope will display market discrepancies here when they meet the signal threshold.
            </span>
        </div>

        {% endif %}

    </section>


<section class="parlay-lab" id="parlay-lab">
  <div class="parlay-head">
    <div><div class="eyebrow">Parlay Lab</div><h2>Parlay Comparison</h2>
      <p>Build a 2–6 leg parlay from exact markets already loaded in OddsScope. We calculate the combined price at every sportsbook that carries every selected leg.</p>
    </div>
    <div class="parlay-badge">CALCULATED PRICES</div>
  </div>
  <div class="parlay-grid">
    <div class="parlay-box">
      <div class="parlay-box-head">Build Your Parlay · <span id="parlay-count">0 / 6 legs</span></div>
      <div class="parlay-form">
        <div class="parlay-field"><label>Game</label><select id="parlay-game"><option value="">Choose a game</option></select></div>
        <div class="parlay-field"><label>Market</label><select id="parlay-market" disabled><option value="">Choose market</option></select></div>
        <div class="parlay-field"><label>Selection</label><select id="parlay-selection" disabled><option value="">Choose selection</option></select></div>
        <button class="parlay-btn" id="parlay-add" type="button">+ Add Leg</button>
      </div>
      <div class="parlay-error" id="parlay-error"></div>
      <div class="parlay-slip" id="parlay-slip"><div class="parlay-empty">Add at least two legs to compare prices.</div></div>
    </div>
    <div class="parlay-box">
      <div class="parlay-box-head">Sportsbook Comparison</div>
      <div class="parlay-tools">
        <div class="parlay-field"><label>Stake</label><input id="parlay-stake" type="number" min="1" value="100"></div>
        <button class="parlay-btn" id="parlay-clear" type="button">Clear</button>
      </div>
      <div class="parlay-results" id="parlay-results"><div class="parlay-empty">Comparison appears after two legs.</div></div>
      <div class="parlay-note"><strong>Calculated price:</strong> individual leg odds are multiplied mathematically. A sportsbook's actual parlay quote can differ because of correlation, same-game restrictions, rounding, or pricing adjustments.</div>
    </div>
  </div>
</section>


    <section class="movement-lab" id="movement-lab">
        <div class="movement-head">
            <div>
                <div class="eyebrow">Live Market Tape</div>
                <h2>Market Movement</h2>
                <p>Watch the market median move through time. Hover any point for its timestamp and value. New snapshots animate onto the chart.</p>
            </div>
            <div class="movement-live"><span class="movement-dot"></span> HISTORY LIVE</div>
        </div>
        <div class="movement-controls">
            <div class="movement-field"><label>Game</label><select id="movement-game"><option value="">Choose a game</option>{% for game in games %}<option value="{{ game.id }}">{{ game.away }} @ {{ game.home }}</option>{% endfor %}</select></div>
            <div class="movement-field"><label>Market</label><select id="movement-market"><option value="spread">Away spread</option><option value="total">Total</option><option value="moneyline">Away moneyline</option></select></div>
            <div class="movement-field"><label>Window</label><select id="movement-range"><option value="6">6 hours</option><option value="24" selected>24 hours</option><option value="72">3 days</option><option value="168">7 days</option></select></div>
            <button class="movement-refresh" id="movement-refresh" type="button">↻ Refresh Chart</button>
        </div>
        <div class="movement-grid">
            <div class="movement-chart-card">
                <div class="movement-message" id="movement-message">Choose a game to load its movement history.</div>
                <div class="movement-chart-wrap"><canvas id="movement-chart"></canvas></div>
            </div>
            <div class="movement-stats">
                <div class="movement-stat"><div class="movement-stat-label">Opening</div><div class="movement-stat-value" id="movement-open">—</div><div class="movement-stat-sub">First snapshot in selected window</div></div>
                <div class="movement-stat"><div class="movement-stat-label">Current</div><div class="movement-stat-value" id="movement-current">—</div><div class="movement-stat-sub" id="movement-current-sub">Latest market median</div></div>
                <div class="movement-stat"><div class="movement-stat-label">Move</div><div class="movement-stat-value" id="movement-move">—</div><div class="movement-stat-sub" id="movement-books">Waiting for history</div><div class="movement-spark"></div></div>
            </div>
        </div>
    </section>

    <!-- ====================================================
         CONTROLS
         ==================================================== -->

    <div class="controls">

        <div class="search">

            <span class="search-icon">
                ⌕
            </span>

            <input
                id="searchInput"
                type="text"
                placeholder="Search team..."
                oninput="applyFilters()"
            >

        </div>


        <div class="filters">

            <button
                class="filter-button active"
                data-filter="all"
                onclick="setFilter('all', this)"
            >
                All Games
            </button>


            <button
                class="filter-button"
                data-filter="odds"
                onclick="setFilter('odds', this)"
            >
                With Odds
            </button>


            <button
                class="filter-button"
                data-filter="multi"
                onclick="setFilter('multi', this)"
            >
                2+ Books
            </button>


            <button
                class="filter-button"
                data-filter="live"
                onclick="setFilter('live', this)"
            >
                Live
            </button>

        </div>

    </div>


    <!-- ====================================================
         GAMES
         ==================================================== -->

    <div id="gamesContainer">


        {% set ns = namespace(last_date='') %}


        {% for game in games %}


            {% if game.date != ns.last_date %}

                <div
                    class="date-divider game-date-divider"
                    data-date="{{ game.date }}"
                >
                    {{ game.date }}
                </div>

                {% set ns.last_date = game.date %}

            {% endif %}


            <div
                class="game-card"
                data-search="{{ game.away|lower }} {{ game.home|lower }}"
                data-books="{{ game.book_count }}"
                data-live="{{ 'true' if game.live else 'false' }}"
                data-date="{{ game.date }}"
            >


                <!-- GAME HEADER -->

                <div class="game-header">

                    <div>

                        <div class="game-time">

                            {% if game.live %}

                                <span style="color:#fb7185;">
                                    ● LIVE
                                </span>

                            {% else %}

                                <span
                                    class="local-time"
                                    data-time="{{ game.iso_time }}"
                                >
                                    {{ game.time }}
                                </span>

                            {% endif %}

                        </div>


                        <div class="matchup">

                            {{ game.away }}

                            <span class="at">
                                @
                            </span>

                            {{ game.home }}

                        </div>

                    </div>


                    <div class="book-count">

                        {{ game.book_count }}

                        {% if game.book_count == 1 %}
                            book
                        {% else %}
                            books
                        {% endif %}

                    </div>

                </div>


                {% if game.book_count > 0 %}


                <!-- ==========================================
                     BEST AVAILABLE
                     ========================================== -->

                <div class="best-strip">

                    <div class="best-label">
                        ★ Best Available
                    </div>


                    <div class="best-grid">


                        <div class="best-item">

                            <div class="best-market">
                                {{ game.away }} ML
                            </div>

                            <div class="best-value">
                                {{ game.best.away_ml }}
                            </div>

                            <div class="best-book">
                                {{ game.best.away_ml_book }}
                            </div>

                        </div>


                        <div class="best-item">

                            <div class="best-market">
                                {{ game.home }} ML
                            </div>

                            <div class="best-value">
                                {{ game.best.home_ml }}
                            </div>

                            <div class="best-book">
                                {{ game.best.home_ml_book }}
                            </div>

                        </div>


                        <div class="best-item">

                            <div class="best-market">
                                {{ game.away }} Spread
                            </div>

                            <div class="best-value">
                                {{ game.best.away_spread }}
                            </div>

                            <div class="best-book">
                                {{ game.best.away_spread_book }}
                            </div>

                        </div>


                        <div class="best-item">

                            <div class="best-market">
                                {{ game.home }} Spread
                            </div>

                            <div class="best-value">
                                {{ game.best.home_spread }}
                            </div>

                            <div class="best-book">
                                {{ game.best.home_spread_book }}
                            </div>

                        </div>


                        <div class="best-item">

                            <div class="best-market">
                                Over
                            </div>

                            <div class="best-value">
                                {{ game.best.over }}
                            </div>

                            <div class="best-book">
                                {{ game.best.over_book }}
                            </div>

                        </div>


                        <div class="best-item">

                            <div class="best-market">
                                Under
                            </div>

                            <div class="best-value">
                                {{ game.best.under }}
                            </div>

                            <div class="best-book">
                                {{ game.best.under_book }}
                            </div>

                        </div>

                    </div>

                </div>


                <!-- ==========================================
                     CONSENSUS
                     ========================================== -->

                <div class="consensus">

                    <div class="consensus-item">

                        <div class="consensus-label">
                            No-Vig Win Probability
                        </div>

                        <div class="consensus-value">

                            {{ game.away }}
                            {{ game.consensus.away_probability_display }}

                            ·

                            {{ game.home }}
                            {{ game.consensus.home_probability_display }}

                        </div>

                    </div>


                    <div class="consensus-item">

                        <div class="consensus-label">
                            Consensus Spread
                        </div>

                        <div class="consensus-value">

                            {{ game.away }}

                            {{ game.consensus.spread_display }}

                        </div>

                    </div>


                    <div class="consensus-item">

                        <div class="consensus-label">
                            Consensus Total
                        </div>

                        <div class="consensus-value">

                            {{ game.consensus.total_display }}

                        </div>

                    </div>


                    <div class="consensus-item">

                        <div class="consensus-label">
                            Avg ML Hold
                        </div>

                        <div class="consensus-value">

                            {{ game.consensus.average_hold_display }}

                        </div>

                    </div>

                </div>


                <!-- ==========================================
                     ODDS TABLE
                     ========================================== -->

                <div class="table-wrapper">

                    <table>

                        <thead>

                            <tr>

                                <th>
                                    Sportsbook
                                </th>

                                <th>
                                    {{ game.away }} ML
                                </th>

                                <th>
                                    {{ game.home }} ML
                                </th>

                                <th>
                                    {{ game.away }} Spread
                                </th>

                                <th>
                                    {{ game.home }} Spread
                                </th>

                                <th>
                                    Over
                                </th>

                                <th>
                                    Under
                                </th>

                            </tr>

                        </thead>


                        <tbody>


                        {% for book in game.books %}

                            <tr>


                                <td class="book-name">

                                    {{ book.name }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_away_ml %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.away_ml_display }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_home_ml %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.home_ml_display }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_away_spread %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.away_spread_display }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_home_spread %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.home_spread_display }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_over %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.over_display }}

                                </td>


                                <td class="
                                    odds-cell
                                    {% if book.best_under %}
                                        best-cell
                                    {% endif %}
                                ">

                                    {{ book.under_display }}

                                </td>


                            </tr>

                        {% endfor %}


                        </tbody>

                    </table>

                </div>


                <!-- ==========================================
                     PROBABILITY ANALYSIS
                     ========================================== -->

                <details>

                    <summary>
                        Market probability analysis
                    </summary>


                    <div class="analysis-content">

                        <div class="analysis-note">

                            No-vig probabilities remove the
                            two-way moneyline overround from
                            each sportsbook. They describe
                            market-implied probability, not
                            a prediction of the actual game.

                        </div>


                        <table class="prob-table">

                            <thead>

                                <tr>

                                    <th>
                                        Sportsbook
                                    </th>

                                    <th>
                                        {{ game.away }}
                                    </th>

                                    <th>
                                        {{ game.home }}
                                    </th>

                                    <th>
                                        ML Hold
                                    </th>

                                </tr>

                            </thead>


                            <tbody>


                            {% for book in game.books %}

                                {% if book.away_no_vig is not none %}

                                    <tr>

                                        <td class="book-name">
                                            {{ book.name }}
                                        </td>

                                        <td>
                                            {{ book.away_no_vig_display }}
                                        </td>

                                        <td>
                                            {{ book.home_no_vig_display }}
                                        </td>

                                        <td>
                                            {{ book.hold_display }}
                                        </td>

                                    </tr>

                                {% endif %}

                            {% endfor %}


                            </tbody>

                        </table>

                    </div>

                </details>


                {% else %}


                <div
                    style="
                        padding:18px 22px;
                        border-top:1px solid #1f2a38;
                        color:#718196;
                        font-size:12px;
                    "
                >

                    No sportsbook markets currently available.

                </div>


                {% endif %}


            </div>


        {% endfor %}


    </div>


    <!-- ====================================================
         EMPTY SEARCH STATE
         ==================================================== -->

    <div
        id="emptyState"
        class="empty"
        style="display:none;"
    >

        No games match your search.

    </div>


    <!-- ====================================================
         FOOTER
         ==================================================== -->

    <div class="footer">

        OddsScope compares sportsbook market data for
        informational and analytical purposes.

        <br><br>

        "Best" refers only to the most favorable currently
        displayed betting line or price. A better sportsbook
        price does not mean that a wager has positive expected
        value.

        <br>

        Market-implied and no-vig probabilities are derived
        from sportsbook prices and should not be interpreted
        as independent game predictions.

        <br><br>

        Data refresh cache:
        {{ cache_seconds }} seconds.

        {% if meta.daily_remaining %}

            · API requests remaining today:
            {{ meta.daily_remaining }}

        {% endif %}

    </div>


</div>


<!-- ========================================================
     JAVASCRIPT
     ======================================================== -->

<script>

    // ======================================================
    // PARLAY LAB
    // ======================================================

    const PARLAY_GAMES = {{ games | tojson }};
    const PARLAY_MAX = 6;
    let parlayLegs = [];

    function pEl(id) {
        return document.getElementById(id);
    }

    function pGame(id) {
        return PARLAY_GAMES.find(
            game => String(game.id) === String(id)
        );
    }

    function pBookKey(book) {
        return String(
            book.key || book.name || ""
        ).trim().toLowerCase();
    }

    function pAmericanToDecimal(odds) {
        const value = Number(odds);

        if (!Number.isFinite(value) || value === 0) {
            return null;
        }

        return value > 0
            ? 1 + value / 100
            : 1 + 100 / Math.abs(value);
    }

    function pDecimalToAmerican(decimalOdds) {
        if (
            !Number.isFinite(decimalOdds)
            || decimalOdds <= 1
        ) {
            return null;
        }

        return Math.round(
            decimalOdds >= 2
                ? (decimalOdds - 1) * 100
                : -100 / (decimalOdds - 1)
        );
    }

    function pOddsText(odds) {
        return odds > 0
            ? `+${odds}`
            : String(odds);
    }

    function pPointText(point) {
        const value = Number(point);

        if (value > 0) {
            return `+${value}`;
        }

        if (value === 0) {
            return "PK";
        }

        return String(value);
    }

    function pMarkets(game) {
        const markets = [];

        if (
            game.books.some(
                book =>
                    book.away_ml != null
                    && book.home_ml != null
            )
        ) {
            markets.push(["ml", "Moneyline"]);
        }

        if (
            game.books.some(
                book =>
                    book.away_spread != null
                    && book.home_spread != null
            )
        ) {
            markets.push(["spread", "Spread"]);
        }

        if (
            game.books.some(
                book =>
                    book.total != null
                    && (
                        book.over_price != null
                        || book.under_price != null
                    )
            )
        ) {
            markets.push(["total", "Total"]);
        }

        return markets;
    }

    function pSelections(game, market) {
        if (market === "ml") {
            return [
                {
                    id: "away_ml",
                    label: `${game.away} ML`,
                    market: "ml",
                    side: "away"
                },
                {
                    id: "home_ml",
                    label: `${game.home} ML`,
                    market: "ml",
                    side: "home"
                }
            ];
        }

        const selections = new Map();

        if (market === "spread") {
            game.books.forEach(book => {
                [
                    ["away", book.away_spread],
                    ["home", book.home_spread]
                ].forEach(([side, point]) => {
                    if (point == null) {
                        return;
                    }

                    const numericPoint = Number(point);
                    const id = `${side}:${numericPoint}`;

                    if (!selections.has(id)) {
                        selections.set(
                            id,
                            {
                                id,
                                label:
                                    `${game[side]} ${pPointText(numericPoint)}`,
                                market: "spread",
                                side,
                                point: numericPoint
                            }
                        );
                    }
                });
            });
        }

        if (market === "total") {
            game.books.forEach(book => {
                if (book.total == null) {
                    return;
                }

                const total = Number(book.total);

                if (
                    book.over_price != null
                    && !selections.has(`over:${total}`)
                ) {
                    selections.set(
                        `over:${total}`,
                        {
                            id: `over:${total}`,
                            label: `Over ${total}`,
                            market: "total",
                            side: "over",
                            point: total
                        }
                    );
                }

                if (
                    book.under_price != null
                    && !selections.has(`under:${total}`)
                ) {
                    selections.set(
                        `under:${total}`,
                        {
                            id: `under:${total}`,
                            label: `Under ${total}`,
                            market: "total",
                            side: "under",
                            point: total
                        }
                    );
                }
            });
        }

        return Array.from(
            selections.values()
        );
    }

    function pPriceForBook(book, leg) {
        if (leg.market === "ml") {
            const price =
                book[`${leg.side}_ml`];

            return price == null
                ? null
                : Number(price);
        }

        if (leg.market === "spread") {
            const point =
                book[`${leg.side}_spread`];

            const price =
                book[
                    `${leg.side}_spread_price`
                ];

            if (
                point == null
                || price == null
                || Math.abs(
                    Number(point) - leg.point
                ) > 0.001
            ) {
                return null;
            }

            return Number(price);
        }

        if (leg.market === "total") {
            const price =
                book[`${leg.side}_price`];

            if (
                book.total == null
                || price == null
                || Math.abs(
                    Number(book.total) - leg.point
                ) > 0.001
            ) {
                return null;
            }

            return Number(price);
        }

        return null;
    }

    function pFillGames() {
        const select = pEl("parlay-game");

        PARLAY_GAMES.forEach(game => {
            const option =
                document.createElement("option");

            option.value =
                String(game.id);

            option.textContent =
                `${game.away} @ ${game.home}`;

            select.appendChild(option);
        });
    }

    function pFillMarkets() {
        const game =
            pGame(pEl("parlay-game").value);

        const market =
            pEl("parlay-market");

        const selection =
            pEl("parlay-selection");

        market.innerHTML =
            '<option value="">Choose market</option>';

        selection.innerHTML =
            '<option value="">Choose selection</option>';

        selection.disabled = true;

        if (!game) {
            market.disabled = true;
            return;
        }

        pMarkets(game).forEach(
            ([value, label]) => {
                const option =
                    document.createElement("option");

                option.value = value;
                option.textContent = label;
                market.appendChild(option);
            }
        );

        market.disabled = false;
    }

    function pFillSelections() {
        const game =
            pGame(pEl("parlay-game").value);

        const market =
            pEl("parlay-market").value;

        const select =
            pEl("parlay-selection");

        select.innerHTML =
            '<option value="">Choose selection</option>';

        if (!game || !market) {
            select.disabled = true;
            return;
        }

        pSelections(
            game,
            market
        ).forEach(item => {
            const option =
                document.createElement("option");

            option.value = item.id;
            option.textContent = item.label;
            select.appendChild(option);
        });

        select.disabled = false;
    }

    function pAddLeg() {
        const error = pEl("parlay-error");
        error.textContent = "";

        if (parlayLegs.length >= PARLAY_MAX) {
            error.textContent =
                "Maximum 6 legs.";
            return;
        }

        const game =
            pGame(pEl("parlay-game").value);

        const market =
            pEl("parlay-market").value;

        const selectionId =
            pEl("parlay-selection").value;

        if (
            !game
            || !market
            || !selectionId
        ) {
            error.textContent =
                "Choose a game, market, and selection.";
            return;
        }

        const selection =
            pSelections(
                game,
                market
            ).find(
                item =>
                    item.id === selectionId
            );

        if (!selection) {
            return;
        }

        const leg = {
            ...selection,
            gameId: String(game.id),
            away: game.away,
            home: game.home
        };

        if (
            parlayLegs.some(
                existing =>
                    existing.gameId === leg.gameId
                    && existing.id === leg.id
            )
        ) {
            error.textContent =
                "That exact leg is already selected.";
            return;
        }

        parlayLegs.push(leg);
        pRender();
    }

    function pComparison() {
        if (parlayLegs.length < 2) {
            return [];
        }

        const firstGame =
            pGame(parlayLegs[0].gameId);

        if (!firstGame) {
            return [];
        }

        const candidates = new Map();

        firstGame.books.forEach(book => {
            const key = pBookKey(book);

            if (key) {
                candidates.set(
                    key,
                    book.name || book.key
                );
            }
        });

        const rows = [];

        candidates.forEach(
            (name, key) => {
                let decimal = 1;

                for (const leg of parlayLegs) {
                    const game =
                        pGame(leg.gameId);

                    const book =
                        game
                        && game.books.find(
                            candidate =>
                                pBookKey(candidate)
                                === key
                        );

                    if (!book) {
                        decimal = null;
                        break;
                    }

                    const price =
                        pPriceForBook(
                            book,
                            leg
                        );

                    const legDecimal =
                        pAmericanToDecimal(
                            price
                        );

                    if (legDecimal == null) {
                        decimal = null;
                        break;
                    }

                    decimal *= legDecimal;
                }

                if (decimal != null) {
                    rows.push({
                        name,
                        decimal,
                        american:
                            pDecimalToAmerican(
                                decimal
                            )
                    });
                }
            }
        );

        return rows.sort(
            (a, b) =>
                b.decimal - a.decimal
        );
    }

    function pRender() {
        pEl("parlay-count").textContent =
            `${parlayLegs.length} / ${PARLAY_MAX} legs`;

        const slip = pEl("parlay-slip");

        if (!parlayLegs.length) {
            slip.innerHTML =
                '<div class="parlay-empty">Add at least two legs to compare prices.</div>';
        } else {
            slip.innerHTML =
                parlayLegs.map(
                    (leg, index) => `
                        <div class="parlay-leg">
                            <div class="parlay-num">
                                ${index + 1}
                            </div>
                            <div>
                                <div class="parlay-game">
                                    ${leg.away} @ ${leg.home}
                                </div>
                                <div class="parlay-title">
                                    ${leg.label}
                                </div>
                            </div>
                            <button
                                class="parlay-remove"
                                type="button"
                                data-rm="${index}"
                            >
                                ×
                            </button>
                        </div>
                    `
                ).join("");

            slip.querySelectorAll(
                "[data-rm]"
            ).forEach(button => {
                button.addEventListener(
                    "click",
                    () => {
                        parlayLegs.splice(
                            Number(
                                button.dataset.rm
                            ),
                            1
                        );

                        pRender();
                    }
                );
            });
        }

        const results =
            pEl("parlay-results");

        if (parlayLegs.length < 2) {
            results.innerHTML =
                '<div class="parlay-empty">Comparison appears after two legs.</div>';
            return;
        }

        const rows = pComparison();

        if (!rows.length) {
            results.innerHTML =
                '<div class="parlay-empty">No single sportsbook has every exact selected leg in the current feed.</div>';
            return;
        }

        let stake =
            Number(
                pEl("parlay-stake").value
            );

        if (
            !Number.isFinite(stake)
            || stake <= 0
        ) {
            stake = 100;
        }

        results.innerHTML = `
            <table class="parlay-table">
                <thead>
                    <tr>
                        <th>Sportsbook</th>
                        <th>Calc. Odds</th>
                        <th>Profit</th>
                        <th>Return</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows.map(
                        (row, index) => `
                            <tr class="${
                                index === 0
                                    ? "parlay-best"
                                    : ""
                            }">
                                <td>
                                    ${row.name}${
                                        index === 0
                                            ? " · BEST"
                                            : ""
                                    }
                                </td>
                                <td>
                                    ${pOddsText(
                                        row.american
                                    )}
                                </td>
                                <td>
                                    $${(
                                        stake
                                        * (
                                            row.decimal - 1
                                        )
                                    ).toFixed(2)}
                                </td>
                                <td>
                                    $${(
                                        stake
                                        * row.decimal
                                    ).toFixed(2)}
                                </td>
                            </tr>
                        `
                    ).join("")}
                </tbody>
            </table>
        `;
    }

    function pInit() {
        if (!pEl("parlay-game")) {
            return;
        }

        pFillGames();

        pEl("parlay-game")
            .addEventListener(
                "change",
                pFillMarkets
            );

        pEl("parlay-market")
            .addEventListener(
                "change",
                pFillSelections
            );

        pEl("parlay-add")
            .addEventListener(
                "click",
                pAddLeg
            );

        pEl("parlay-clear")
            .addEventListener(
                "click",
                () => {
                    parlayLegs = [];
                    pEl(
                        "parlay-error"
                    ).textContent = "";
                    pRender();
                }
            );

        pEl("parlay-stake")
            .addEventListener(
                "input",
                pRender
            );

        pRender();
    }

    pInit();



    // MARKET MOVEMENT
    let movementChart = null;

    function movementFormatValue(value, market) {
        if (value == null || !Number.isFinite(Number(value))) return "—";
        const n = Number(value);
        if (market === "spread" || market === "moneyline") return n > 0 ? `+${n}` : String(n);
        return String(n);
    }

    function movementTimeLabel(iso) {
        const d = new Date(iso);
        return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
    }

    async function loadMovementChart() {
        const gameId = document.getElementById("movement-game").value;
        const market = document.getElementById("movement-market").value;
        const hours = document.getElementById("movement-range").value;
        const message = document.getElementById("movement-message");

        if (!gameId) {
            message.style.display = "grid";
            message.textContent = "Choose a game to load its movement history.";
            return;
        }

        message.style.display = "grid";
        message.textContent = "Loading market history…";

        try {
            const response = await fetch(`/api/history/${encodeURIComponent(gameId)}?market=${encodeURIComponent(market)}&hours=${encodeURIComponent(hours)}`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || "History request failed");

            const points = data.points || [];
            if (!points.length) {
                if (movementChart) { movementChart.destroy(); movementChart = null; }
                message.style.display = "grid";
                message.textContent = "No snapshots yet for this game/window. Refresh OddsScope a few times as the market changes and this graph will come alive.";
                ["movement-open","movement-current","movement-move"].forEach(id => document.getElementById(id).textContent = "—");
                document.getElementById("movement-books").textContent = "No history yet";
                return;
            }

            message.style.display = "none";
            const labels = points.map(p => movementTimeLabel(p.time));
            const values = points.map(p => p.value);
            const counts = points.map(p => p.books);
            const ctx = document.getElementById("movement-chart").getContext("2d");

            if (movementChart) movementChart.destroy();

            movementChart = new Chart(ctx, {
                type: "line",
                data: {
                    labels,
                    datasets: [{
                        label: data.label,
                        data: values,
                        borderWidth: 2,
                        pointRadius: 3,
                        pointHoverRadius: 6,
                        tension: 0.28,
                        fill: true,
                        borderColor: "#34d399",
                        backgroundColor: "rgba(52,211,153,0.08)",
                        pointBackgroundColor: "#93c5fd",
                        pointBorderColor: "#0d131d"
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: {duration: 850, easing: "easeOutQuart"},
                    interaction: {intersect: false, mode: "index"},
                    plugins: {
                        legend: {labels: {color:"#8d9bad", boxWidth:10, boxHeight:2, font:{size:10}}},
                        tooltip: {callbacks: {afterLabel: c => `${counts[c.dataIndex]} books in median`}}
                    },
                    scales: {
                        x: {ticks:{color:"#64748b",maxTicksLimit:8,font:{size:9}},grid:{color:"rgba(148,163,184,0.06)"}},
                        y: {ticks:{color:"#64748b",font:{size:9},callback:v=>movementFormatValue(v,market)},grid:{color:"rgba(148,163,184,0.08)"}}
                    }
                }
            });

            const first = Number(points[0].value);
            const last = Number(points[points.length - 1].value);
            const move = last - first;

            document.getElementById("movement-open").textContent = movementFormatValue(first, market);
            document.getElementById("movement-current").textContent = movementFormatValue(last, market);
            document.getElementById("movement-move").textContent = Math.abs(move) < .0001 ? "0" : `${move > 0 ? "▲ +" : "▼ "}${Number(Math.abs(move).toFixed(2))}`;
            document.getElementById("movement-current-sub").textContent = `${points.length} market changes in chart`;
            document.getElementById("movement-books").textContent = `${points[points.length - 1].books} books in latest median`;
        } catch (error) {
            message.style.display = "grid";
            message.textContent = `Could not load history: ${error.message}`;
        }
    }

    document.getElementById("movement-game")?.addEventListener("change", loadMovementChart);
    document.getElementById("movement-market")?.addEventListener("change", loadMovementChart);
    document.getElementById("movement-range")?.addEventListener("change", loadMovementChart);
    document.getElementById("movement-refresh")?.addEventListener("click", loadMovementChart);


    let activeFilter = "all";


    // ======================================================
    // SIGNAL DETAILS
    // ======================================================

    function toggleSignalDetails(button) {

        const row =
            button.closest(".signal-row");

        if (!row) {
            return;
        }

        row.classList.toggle(
            "expanded"
        );

        const arrow =
            button.querySelector("span");

        if (arrow) {

            arrow.textContent =
                row.classList.contains("expanded")
                ? "⌃"
                : "⌄";

        }

    }


    // ======================================================
    // LOCAL TIME
    // ======================================================

    document
        .querySelectorAll(".local-time")
        .forEach(element => {

            const iso =
                element.dataset.time;

            if (!iso) {
                return;
            }

            const date =
                new Date(iso);

            if (
                Number.isNaN(
                    date.getTime()
                )
            ) {
                return;
            }

            element.textContent =
                new Intl.DateTimeFormat(
                    undefined,
                    {
                        weekday:
                            "short",

                        month:
                            "short",

                        day:
                            "numeric",

                        hour:
                            "numeric",

                        minute:
                            "2-digit"
                    }
                ).format(date);

        });


    // ======================================================
    // FILTER BUTTONS
    // ======================================================

    function setFilter(
        filter,
        button
    ) {

        activeFilter =
            filter;


        document
            .querySelectorAll(
                ".filter-button"
            )
            .forEach(btn => {

                btn.classList.remove(
                    "active"
                );

            });


        button.classList.add(
            "active"
        );


        applyFilters();

    }


    // ======================================================
    // SEARCH + FILTER
    // ======================================================

    function applyFilters() {

        const query =
            document
                .getElementById(
                    "searchInput"
                )
                .value
                .toLowerCase()
                .trim();


        const cards =
            document
                .querySelectorAll(
                    ".game-card"
                );


        let visibleCount = 0;


        cards.forEach(card => {

            const searchText =
                card.dataset.search || "";

            const books =
                Number(
                    card.dataset.books || 0
                );

            const live =
                card.dataset.live === "true";


            const matchesSearch =
                !query
                ||
                searchText.includes(
                    query
                );


            let matchesFilter =
                true;


            if (
                activeFilter === "odds"
            ) {

                matchesFilter =
                    books > 0;

            }


            else if (
                activeFilter === "multi"
            ) {

                matchesFilter =
                    books >= 2;

            }


            else if (
                activeFilter === "live"
            ) {

                matchesFilter =
                    live;

            }


            const visible =
                matchesSearch
                &&
                matchesFilter;


            card.style.display =
                visible
                ? ""
                : "none";


            if (visible) {

                visibleCount += 1;

            }

        });


        updateDateDividers();


        document
            .getElementById(
                "emptyState"
            )
            .style
            .display =
                visibleCount === 0
                ? "block"
                : "none";

    }


    // ======================================================
    // HIDE EMPTY DATE HEADINGS
    // ======================================================

    function updateDateDividers() {

        const dividers =
            document
                .querySelectorAll(
                    ".game-date-divider"
                );


        dividers.forEach(
            divider => {

                const date =
                    divider.dataset.date;


                const cards =
                    document
                        .querySelectorAll(
                            `.game-card[data-date="${date}"]`
                        );


                let anyVisible =
                    false;


                cards.forEach(
                    card => {

                        if (
                            card.style.display
                            !== "none"
                        ) {

                            anyVisible =
                                true;

                        }

                    }
                );


                divider.style.display =
                    anyVisible
                    ? ""
                    : "none";

            }
        );

    }

</script>


</body>

</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def home():

    try:

        games, meta = fetch_games()

        signals = analyze_games(games)

        print( "MARKET SIGNALS FOUND:",
                len(signals))

        

        return render_template_string(

            HTML,
            games=games,
            meta=meta,
            signals=signals,
            cache_seconds=CACHE_SECONDS
        )


    except Exception as error:

        print(
            "HOME ERROR:",
            repr(error)
        )

        return f"""
        <!DOCTYPE html>

        <html>

        <body style="
            margin:0;
            background:#070b12;
            color:white;
            font-family:Arial;
            padding:40px;
        ">

            <h1 style="
                color:#fb7185;
            ">
                OddsScope Error
            </h1>

            <p>
                The application could not load
                sportsbook data.
            </p>

            <pre style="
                background:#111925;
                border:1px solid #1f2a38;
                padding:20px;
                border-radius:10px;
                overflow:auto;
            ">{repr(error)}</pre>

        </body>

        </html>
        """, 500



@app.route("/api/history/<event_id>")
def api_history(event_id):
    """Return robust median market history from saved SQLite snapshots."""
    try:
        market = str(request.args.get("market", "spread")).lower()
        hours = max(1, min(int(request.args.get("hours", 24)), 24 * 14))

        columns = {
            "spread": "away_spread",
            "total": "total",
            "moneyline": "away_ml"
        }

        if market not in columns:
            return jsonify({"error": "Unsupported market"}), 400

        db_path = BASE_DIR / "oddsscope.db"
        if not db_path.exists():
            return jsonify({"event_id": event_id, "market": market, "points": []})

        column = columns[market]
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row

        rows = connection.execute(
            f"""
            SELECT recorded_at, sportsbook_name, {column} AS market_value
            FROM odds_history
            WHERE event_id = ?
              AND {column} IS NOT NULL
              AND datetime(recorded_at) >= datetime('now', ?)
            ORDER BY datetime(recorded_at) ASC
            """,
            (event_id, f"-{hours} hours")
        ).fetchall()
        connection.close()

        grouped = defaultdict(list)
        for row in rows:
            try:
                value = float(row["market_value"])
            except (TypeError, ValueError):
                continue
            stamp = str(row["recorded_at"])
            minute = stamp[:16] + ":00"
            grouped[minute].append(value)

        points = []
        for stamp in sorted(grouped):
            values = sorted(grouped[stamp])
            if not values:
                continue
            mid = len(values) // 2
            median_value = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
            point = {"time": stamp, "value": round(median_value, 2), "books": len(values)}

            if points and abs(points[-1]["value"] - point["value"]) < .0001:
                points[-1] = point
            else:
                points.append(point)

        if len(points) > 180:
            step = math.ceil(len(points) / 180)
            sampled = points[::step]
            if sampled[-1] != points[-1]:
                sampled.append(points[-1])
            points = sampled

        labels = {
            "spread": "Away spread · market median",
            "total": "Game total · market median",
            "moneyline": "Away moneyline · market median"
        }

        return jsonify({
            "event_id": event_id,
            "market": market,
            "label": labels[market],
            "hours": hours,
            "points": points
        })

    except Exception as error:
        print("HISTORY API ERROR:", repr(error))
        return jsonify({"error": repr(error)}), 500


@app.route("/api/games")
def api_games():
    """
    Optional JSON endpoint.

    Useful later if we move the frontend into separate
    JavaScript files.
    """

    try:

        games, meta = fetch_games()

        return jsonify({
            "meta": meta,
            "games": games
        })

    except Exception as error:

        return jsonify({
            "error": repr(error)
        }), 500


@app.route("/refresh")
def refresh():
    """
    Force a new PropLine request.

    Don't repeatedly spam this route because it intentionally
    bypasses the cache.
    """

    try:

        games, meta = fetch_games(
            force=True
        )

        return jsonify({
            "status": "success",
            "games": len(games),
            "sportsbooks":
                meta[
                    "sportsbook_count"
                ],
            "daily_remaining":
                meta.get(
                    "daily_remaining"
                )
        })

    except Exception as error:

        return jsonify({
            "error": repr(error)
        }), 500


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print()
    print("======================================")
    print("             ODDSSCOPE")
    print("======================================")
    print()

    if API_KEY:

        print(
            "✓ PropLine API key loaded"
        )

    else:

        print(
            "✗ PROPLINE_API_KEY not found"
        )


    print()

    print(
        "Dashboard:"
    )

    print(
        "http://127.0.0.1:5000"
    )

    print()

    print(
        "JSON:"
    )

    print(
        "http://127.0.0.1:5000/api/games"
    )

    print()

    print(
        "Cache:",
        CACHE_SECONDS,
        "seconds"
    )

    print()


    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )