# ============================================================

# ODDSSCOPE SIGNAL ENGINE

# ============================================================

#

# This module analyzes CURRENT sportsbook markets.

#

# It currently detects:

#

#   1. Moneyline price dislocations

#   2. Spread price advantages at identical lines

#   3. Total price advantages at identical lines

#   4. Two-way moneyline arbitrage opportunities

#

# IMPORTANT:

#

# A market signal is NOT automatically a profitable bet.

# We are measuring differences between sportsbook prices.

#

# ============================================================



SIGNAL_EXCLUDED_BOOKS = {

    "kalshi",

    "polymarket",

    "polymarket us",

    "prophetx",

    "novig"

}





def signal_eligible_books(game):

    """

    Return books that we currently trust for automated

    cross-sportsbook signal calculations.



    Exchange / prediction-market feeds are still displayed

    by OddsScope, but are excluded from Signals because their

    market structure can differ from conventional sportsbook

    two-way markets.

    """



    eligible = []



    for book in game.get("books", []):



        name = str(

            book.get("name", "")

        ).strip().lower()



        if name in SIGNAL_EXCLUDED_BOOKS:

            continue



        eligible.append(book)



    return eligible

def implied_probability(odds):

    """

    Convert American odds into implied probability.

    """



    if odds is None or odds == 0:

        return None



    odds = float(odds)



    if odds < 0:

        return abs(odds) / (abs(odds) + 100)



    return 100 / (odds + 100)





def american_profit(odds, stake=100):

    """

    Profit returned on a winning American-odds wager.



    +150 with $100 stake -> $150 profit

    -150 with $100 stake -> $66.67 profit

    """



    if odds is None:

        return None



    odds = float(odds)



    if odds > 0:

        return stake * (odds / 100)



    return stake * (100 / abs(odds))





def no_vig_probabilities(odds1, odds2):

    """

    Remove two-way market vig.

    """



    p1 = implied_probability(odds1)

    p2 = implied_probability(odds2)



    if p1 is None or p2 is None:

        return None, None



    total = p1 + p2



    if total <= 0:

        return None, None



    return (

        p1 / total,

        p2 / total

    )





def median(values):

    """

    Small median helper.

    """



    values = sorted(

        value

        for value in values

        if value is not None

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





# ============================================================

# MONEYLINE MARKET CONSENSUS

# ============================================================



def moneyline_consensus(

    books,

    side

):

    """

    Estimate consensus no-vig win probability.



    side must be:

        "away"

        "home"



    We calculate each book's no-vig probability first,

    then take the median.



    Median is intentionally used instead of mean because

    one weird sportsbook price has less ability to distort it.

    """



    probabilities = []



    for book in books:



        away_odds = book.get("away_ml")

        home_odds = book.get("home_ml")



        if (

            away_odds is None

            or home_odds is None

        ):

            continue



        away_prob, home_prob = (

            no_vig_probabilities(

                away_odds,

                home_odds

            )

        )



        if side == "away":

            probability = away_prob

        else:

            probability = home_prob



        if probability is not None:

            probabilities.append(

                probability

            )



    return median(probabilities)





# ============================================================

# MONEYLINE PRICE SIGNALS

# ============================================================



def find_moneyline_signals(game):

    """

    Find sportsbook prices that are favorable relative to

    the broader no-vig market consensus.



    edge = consensus probability - break-even probability



    Example:



        consensus probability = 58%

        sportsbook break-even = 54%



        difference = +4 percentage points



    This is a MARKET-DERIVED signal.

    It is not an independent prediction.

    """



    books = signal_eligible_books(game)



    if len(books) < 3:

        return []



    away_team = game.get("away")

    home_team = game.get("home")



    away_consensus = moneyline_consensus(

        books,

        "away"

    )



    home_consensus = moneyline_consensus(

        books,

        "home"

    )



    signals = []



    sides = [

        (

            "away",

            away_team,

            away_consensus

        ),

        (

            "home",

            home_team,

            home_consensus

        )

    ]



    for side, team, consensus in sides:



        if consensus is None:

            continue



        odds_key = f"{side}_ml"



        for book in books:



            odds = book.get(

                odds_key

            )



            if odds is None:

                continue



            break_even = (

                implied_probability(

                    odds

                )

            )



            if break_even is None:

                continue



            edge = (

                consensus

                -

                break_even

            )



            # Ignore tiny differences.

            #

            # 0.015 = 1.5 percentage points.

            if edge < 0.015:

                continue



            if edge >= 0.04:

                strength = "strong"



            elif edge >= 0.025:

                strength = "medium"



            else:

                strength = "small"



            signals.append({



                "type":

                    "moneyline_value",



                "category":

                    "Price Dislocation",



                "strength":

                    strength,



                "game_id":

                    game.get("id"),



                "away":

                    away_team,



                "home":

                    home_team,



                "team":

                    team,



                "side":

                    side,



                "sportsbook":

                    book.get("name"),



                "sportsbook_key":

                    book.get("key"),



                "odds":

                    odds,



                "odds_display":

                    (

                        f"+{odds}"

                        if odds > 0

                        else str(odds)

                    ),



                "market_probability":

                    consensus,



                "market_probability_display":

                    f"{consensus * 100:.1f}%",



                "break_even":

                    break_even,



                "break_even_display":

                    f"{break_even * 100:.1f}%",



                "edge":

                    edge,



                "edge_display":

                    f"+{edge * 100:.1f} pts",



                "book_count":

                    len(books),



                "title":

                    f"{team} ML",



                "description":

                    (

                        f"{book.get('name')} is offering "

                        f"{team} at a price whose break-even "

                        f"probability is below the broader "

                        f"no-vig market estimate."

                    )

            })



    return signals





# ============================================================

# SPREAD SIGNALS

# ============================================================



def find_spread_signals(game):

    """

    Compare sportsbook prices ONLY when they offer the

    exact same spread.



    This avoids comparing something silly like:

        +20.5

    against:

        +2.5

    """



    books = signal_eligible_books(game)



    signals = []



    for side in ["away", "home"]:



        team = game.get(side)



        groups = {}



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



            groups.setdefault(

                point,

                []

            ).append({

                "book": book,

                "price": price

            })



        for point, offers in groups.items():



            # Need multiple books at identical line

            # before calling anything a market comparison.

            if len(offers) < 3:

                continue



            probabilities = [

                implied_probability(

                    offer["price"]

                )

                for offer in offers

            ]



            probabilities = [

                probability

                for probability in probabilities

                if probability is not None

            ]



            if not probabilities:

                continue



            typical_probability = median(

                probabilities

            )



            for offer in offers:



                book = offer["book"]

                price = offer["price"]



                break_even = (

                    implied_probability(

                        price

                    )

                )



                edge = (

                    typical_probability

                    -

                    break_even

                )



                if edge < 0.015:

                    continue



                if point > 0:

                    point_display = f"+{point:g}"



                elif point == 0:

                    point_display = "PK"



                else:

                    point_display = f"{point:g}"



                signals.append({



                    "type":

                        "spread_price",



                    "category":

                        "Spread Price",



                    "strength":

                        (

                            "strong"

                            if edge >= 0.04

                            else

                            "medium"

                            if edge >= 0.025

                            else

                            "small"

                        ),



                    "game_id":

                        game.get("id"),



                    "away":

                        game.get("away"),



                    "home":

                        game.get("home"),



                    "team":

                        team,



                    "sportsbook":

                        book.get("name"),



                    "sportsbook_key":

                        book.get("key"),



                    "line":

                        point,



                    "line_display":

                        point_display,



                    "odds":

                        price,



                    "odds_display":

                        (

                            f"+{price}"

                            if price > 0

                            else str(price)

                        ),



                    "edge":

                        edge,



                    "edge_display":

                        f"+{edge * 100:.1f} pts",



                    "book_count":

                        len(offers),



                    "title":

                        f"{team} {point_display}",



                    "description":

                        (

                            f"{book.get('name')} has a more "

                            f"favorable price than the median "

                            f"price from books offering the "

                            f"same {point_display} spread."

                        )

                })



    return signals





# ============================================================

# TOTAL SIGNALS

# ============================================================



def find_total_signals(game):

    """

    Compare prices at IDENTICAL total numbers.



    Over 50.5 is compared with other Over 50.5 prices.

    Under 50.5 is compared with other Under 50.5 prices.



    We do NOT compare Over 49.5 with Over 55.5.

    """



    books = signal_eligible_books(game)



    signals = []



    for side in ["over", "under"]:



        groups = {}



        for book in books:



            total = book.get("total")



            price = book.get(

                f"{side}_price"

            )



            if (

                total is None

                or price is None

            ):

                continue



            groups.setdefault(

                total,

                []

            ).append({

                "book": book,

                "price": price

            })



        for total, offers in groups.items():



            if len(offers) < 3:

                continue



            probabilities = [

                implied_probability(

                    offer["price"]

                )

                for offer in offers

            ]



            probabilities = [

                probability

                for probability in probabilities

                if probability is not None

            ]



            if not probabilities:

                continue



            typical_probability = median(

                probabilities

            )



            for offer in offers:



                book = offer["book"]

                price = offer["price"]



                break_even = (

                    implied_probability(

                        price

                    )

                )



                edge = (

                    typical_probability

                    -

                    break_even

                )



                if edge < 0.015:

                    continue



                side_name = (

                    "Over"

                    if side == "over"

                    else "Under"

                )



                signals.append({



                    "type":

                        "total_price",



                    "category":

                        "Total Price",



                    "strength":

                        (

                            "strong"

                            if edge >= 0.04

                            else

                            "medium"

                            if edge >= 0.025

                            else

                            "small"

                        ),



                    "game_id":

                        game.get("id"),



                    "away":

                        game.get("away"),



                    "home":

                        game.get("home"),



                    "team":

                        side_name,



                    "sportsbook":

                        book.get("name"),



                    "sportsbook_key":

                        book.get("key"),



                    "line":

                        total,



                    "line_display":

                        f"{total:g}",



                    "odds":

                        price,



                    "odds_display":

                        (

                            f"+{price}"

                            if price > 0

                            else str(price)

                        ),



                    "edge":

                        edge,



                    "edge_display":

                        f"+{edge * 100:.1f} pts",



                    "book_count":

                        len(offers),



                    "title":

                        f"{side_name} {total:g}",



                    "description":

                        (

                            f"{book.get('name')} has a more "

                            f"favorable price than the median "

                            f"price from books offering the "

                            f"same {total:g} total."

                        )

                })



    return signals





# ============================================================

# ARBITRAGE

# ============================================================



def find_moneyline_arbitrage(game):

    """

    Look for a two-way moneyline arbitrage.



    We find:

        best away ML

        best home ML



    If:



        implied(best away)

        +

        implied(best home)

        < 1



    then the prices mathematically create a theoretical

    two-sided arbitrage before execution constraints.

    """



    books = signal_eligible_books(game)



    away_offers = []

    home_offers = []



    for book in books:



        away_odds = book.get(

            "away_ml"

        )



        home_odds = book.get(

            "home_ml"

        )



        if away_odds is not None:



            away_offers.append({

                "book": book,

                "odds": away_odds

            })



        if home_odds is not None:



            home_offers.append({

                "book": book,

                "odds": home_odds

            })



    if not away_offers or not home_offers:

        return []



    best_away = max(

        away_offers,

        key=lambda offer:

            offer["odds"]

    )



    best_home = max(

        home_offers,

        key=lambda offer:

            offer["odds"]

    )



    # A cross-book arbitrage signal should use opposing

    # prices from different sportsbooks.

    if (

    best_away["book"].get("key")

    ==

    best_home["book"].get("key")

    ):

        return []



    away_probability = (

        implied_probability(

            best_away["odds"]

        )

    )



    home_probability = (

        implied_probability(

            best_home["odds"]

        )

    )



    if (

        away_probability is None

        or home_probability is None

    ):

        return []



    combined = (

        away_probability

        +

        home_probability

    )



    if combined >= 1:

        return []



    theoretical_margin = (

        1 - combined

    )



    return [{



        "type":

            "moneyline_arbitrage",



        "category":

            "Arbitrage",



        "strength":

            "strong",



        "game_id":

            game.get("id"),



        "away":

            game.get("away"),



        "home":

            game.get("home"),



        "away_book":

            best_away[

                "book"

            ].get("name"),



        "away_odds":

            best_away["odds"],



        "away_odds_display":

            (

                f"+{best_away['odds']}"

                if best_away["odds"] > 0

                else str(best_away["odds"])

            ),



        "home_book":

            best_home[

                "book"

            ].get("name"),



        "home_odds":

            best_home["odds"],



        "home_odds_display":

            (

                f"+{best_home['odds']}"

                if best_home["odds"] > 0

                else str(best_home["odds"])

            ),



        "combined_probability":

            combined,



        "combined_probability_display":

            f"{combined * 100:.2f}%",



        "edge":

            theoretical_margin,



        "edge_display":

            f"{theoretical_margin * 100:.2f}%",



        "title":

            (

                f"{game.get('away')} / "

                f"{game.get('home')}"

            ),



        "description":

            (

                "The best opposing moneyline prices "

                "produce a combined implied probability "

                "below 100%. Execution, limits, market "

                "movement and sportsbook rules can affect "

                "whether the opportunity is realizable."

            )

    }]





# ============================================================

# MASTER SIGNAL SCANNER

# ============================================================



def analyze_game(game):

    """

    Run every signal detector for one game.

    """



    signals = []



    signals.extend(

        find_moneyline_signals(

            game

        )

    )



    signals.extend(

        find_spread_signals(

            game

        )

    )



    signals.extend(

        find_total_signals(

            game

        )

    )



    signals.extend(

        find_moneyline_arbitrage(

            game

        )

    )



    return signals





def analyze_games(games):

    """

    Analyze every current game and return one sorted

    signal list.

    """



    signals = []



    for game in games:



        try:



            signals.extend(

                analyze_game(

                    game

                )

            )



        except Exception as error:



            print(

                "SIGNAL ERROR:",

                game.get("id"),

                repr(error)

            )



    # Largest discrepancy first.

    # ========================================================

    # DEDUPLICATE SIGNALS

    # ========================================================



    best_signals = {}



    for signal in signals:



        signal_type = signal.get("type")

        game_id = signal.get("game_id")



        # Arbitrage is already game-level.

        if signal_type == "moneyline_arbitrage":



            key = (

                game_id,

                signal_type

            )



        else:



            key = (

                game_id,

                signal_type,

                signal.get("team"),

                signal.get("line")

            )



        existing = best_signals.get(key)



        if (

            existing is None

            or signal.get("edge", 0)

            >

            existing.get("edge", 0)

        ):

            best_signals[key] = signal





    signals = list(

        best_signals.values()

    )





    signals.sort(

        key=lambda signal:

            signal.get(

                "edge",

                0

            ),

        reverse=True

    )





    return signals
