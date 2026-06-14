"""Offline unit tests for futures_fund.vendors.tag_instruments — precise coin tagging.

The intake layer routes EVERY adapter's text through tag_instruments (via sources/base.py), so a
sloppy substring match poisons whole streams: the Reddit submission footer "[link] [comments]",
"KuCoin-linked", "Iran-Linked" must NOT tag LINK; "tether"/"ethena" must NOT tag ETH. We assert the
whole-token (cashtag / bare ticker / full name) matcher draws those lines correctly. No network.
"""
from __future__ import annotations

# Importing the source-adapter base merges the full-universe alias map (chainlink/litecoin/…) into
# vendors._ALIASES, exactly as production does — every adapter routes tagging through that path.
import futures_fund.sources.base  # noqa: F401  (import for the alias-merge side effect)
from futures_fund.vendors import tag_instruments

# A representative universe in the unified / base forms the desk actually passes in.
UNIVERSE = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "LINK/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "BNB/USDT:USDT", "LTC/USDT:USDT",
]


# -- LINK: the headline offender -------------------------------------------------------------- #

def test_reddit_footer_does_not_tag_link() -> None:
    assert "LINK" not in tag_instruments("Big BTC news today [link] [comments]", UNIVERSE)


def test_linked_word_does_not_tag_link() -> None:
    assert "LINK" not in tag_instruments("KuCoin-linked wallet drained overnight", UNIVERSE)
    assert "LINK" not in tag_instruments("Iran-Linked group moves funds", UNIVERSE)
    assert "LINK" not in tag_instruments("the blockchain interlinked these blinking nodes",
                                         UNIVERSE)
    assert "LINK" not in tag_instruments("two links posted below", UNIVERSE)


def test_cashtag_and_name_and_token_tag_link() -> None:
    assert "LINK" in tag_instruments("$LINK looking strong", UNIVERSE)
    assert "LINK" in tag_instruments("Chainlink ships CCIP upgrade", UNIVERSE)
    assert "LINK" in tag_instruments("LINK rallies 12% on the day", UNIVERSE)
    assert "LINK" in tag_instruments("watching LINK.X on stocktwits", UNIVERSE)


# -- ETH: tether / ethena substrings --------------------------------------------------------- #

def test_tether_does_not_tag_eth() -> None:
    assert "ETH" not in tag_instruments("200M USDT transferred to Bitfinex", UNIVERSE)
    assert "ETH" not in tag_instruments("Tether mints another billion", UNIVERSE)
    assert "ETH" not in tag_instruments("Ethena's USDe yield climbs", UNIVERSE)
    assert "ETH" not in tag_instruments("together they decide whether to sell", UNIVERSE)


def test_cashtag_and_name_and_token_tag_eth() -> None:
    assert "ETH" in tag_instruments("$ETH breaks resistance", UNIVERSE)
    assert "ETH" in tag_instruments("Ethereum gas fees spike", UNIVERSE)
    assert "ETH" in tag_instruments("ETH dumps hard after the open", UNIVERSE)
    assert "ETH" in tag_instruments("ETH.X stream is bullish", UNIVERSE)


# -- cashtag suffix form used by StockTwits --------------------------------------------------- #

def test_dollar_dot_x_cashtag_tags_base() -> None:
    assert tag_instruments("$SOL.X", UNIVERSE) == ["SOL"]
    assert "BTC" in tag_instruments("$BTC.X printing", UNIVERSE)


# -- general precision / regression ---------------------------------------------------------- #

def test_no_substring_false_positives_across_tickers() -> None:
    # "ADA" must not fire on "Canada"; "BNB" not on arbitrary text; "SOL" not on "absolutely".
    assert "ADA" not in tag_instruments("Canada announces crypto rules", UNIVERSE)
    assert "SOL" not in tag_instruments("the deal is absolutely off", UNIVERSE)
    assert "DOGE" not in tag_instruments("dodgers win the series", UNIVERSE)


def test_full_names_tag_correctly() -> None:
    out = tag_instruments("Bitcoin and Ethereum lead; Solana and Cardano follow", UNIVERSE)
    assert set(out) >= {"BTC", "ETH", "SOL", "ADA"}


def test_binance_coin_and_bnb_both_tag_bnb() -> None:
    assert "BNB" in tag_instruments("Binance Coin pumps", UNIVERSE)
    assert "BNB" in tag_instruments("BNB pumps", UNIVERSE)


def test_signature_and_return_shape_preserved() -> None:
    # whole-token, case-insensitive, de-duplicated, order follows the universe.
    out = tag_instruments("ETH eth Ethereum $ETH", UNIVERSE)
    assert out == ["ETH"]
    assert tag_instruments("nothing relevant here", UNIVERSE) == []


def test_empty_and_none_text_safe() -> None:
    assert tag_instruments("", UNIVERSE) == []
