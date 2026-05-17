"""Optedge — config & shared constants."""
from datetime import datetime, timezone

# ---- Universe ----------------------------------------------------------
# Wide coverage. Pulled from S&P 500 + Russell 2000 popular constituents +
# WSB trending names. WSB trending is added DYNAMICALLY at runtime.

LARGE_CAPS = [
    # ETFs
    "SPY", "QQQ", "IWM", "DIA", "ARKK", "ARKG", "ARKW", "XLF", "XLE", "XLK",
    "XLV", "XLY", "XLU", "XLI", "XLP", "XLB", "XLRE", "XOP", "GDX", "SLV",
    "GLD", "TLT", "HYG", "LQD", "EEM", "EFA", "VEA", "VWO",
    # Mega tech
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "ORCL",
    # Large tech / semis / cloud
    "AMD", "INTC", "NFLX", "CRM", "ADBE", "CSCO", "IBM", "QCOM", "TXN", "MU",
    "NOW", "INTU", "PANW", "ANET", "ADI", "LRCX", "KLAC", "MRVL", "ON", "MCHP",
    "AMAT", "ASML", "TSM",
    "SNOW", "CRWD", "NET", "MDB", "DDOG", "WDAY", "SHOP", "TEAM", "ZS", "OKTA",
    "DOCU", "TWLO", "ZM", "FSLY", "DOCN",
    # Banks / fintech
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "SCHW", "COF",
    "AXP", "V", "MA", "PYPL", "BLK", "BX", "KKR", "TROW", "STT", "BK",
    # Insurers
    "MET", "PRU", "AFL", "ALL", "TRV", "CB", "AIG",
    # Energy
    "XOM", "CVX", "COP", "OXY", "EOG", "SLB", "MPC", "VLO", "PSX", "HAL",
    "FANG", "DVN", "PXD", "BKR", "WMB", "KMI", "ENB", "ET",
    # Industrials
    "BA", "CAT", "DE", "HON", "GE", "MMM", "RTX", "LMT", "NOC", "GD",
    "UPS", "FDX", "EMR", "ETN", "ITW", "PH", "ROK", "JCI", "CMI", "PCAR",
    "CSX", "NSC", "UNP", "WM", "RSG",
    # Consumer (staples + discretionary)
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "KO", "PEP",
    "PG", "CL", "KMB", "MO", "PM", "EL", "CHD", "CLX", "GIS",
    "DG", "DLTR", "BBY", "TJX", "ROST", "ULTA", "LULU", "DECK",
    "YUM", "CMG", "QSR", "DPZ",
    # Healthcare / pharma / biotech (large)
    "UNH", "LLY", "JNJ", "PFE", "MRK", "ABBV", "BMY", "GILD", "AMGN", "MDT",
    "TMO", "DHR", "CVS", "ABT", "ISRG", "REGN", "VRTX", "BIIB", "ZTS", "BSX",
    "EW", "CI", "ELV", "HCA", "HUM",
    # Auto (large)
    "F", "GM", "TM", "STLA",
    # Travel / hospitality
    "DAL", "AAL", "UAL", "LUV", "BKNG", "ABNB", "MAR", "HLT", "MGM", "WYNN",
    "LVS", "CCL", "RCL", "NCLH",
    # Telecom / media
    "T", "VZ", "TMUS", "CMCSA", "DIS", "WBD", "NFLX", "PARA", "FOX", "FOXA",
    # Real estate
    "AMT", "EQIX", "PLD", "SPG", "O", "CCI", "PSA", "SBAC", "DLR", "VICI",
    # Materials / chemicals
    "LIN", "APD", "SHW", "ECL", "FCX", "NEM", "AA", "X", "CLF", "STLD", "NUE",
]

SMALL_MID_CAPS_OPTIONS = [
    # Speculative growth / fintech / consumer tech
    "PLTR", "SOFI", "HOOD", "COIN", "AFRM", "UPST", "RBLX", "U", "PINS", "SNAP",
    "DASH", "UBER", "LYFT", "ABNB", "BMBL", "MTCH", "RIVN", "LCID",
    # Crypto-adjacent
    "MARA", "RIOT", "CLSK", "MSTR", "HUT", "WULF", "CIFR", "CORZ", "BITF", "HIVE",
    "GBTC", "IBIT", "ETHA", "BITO",
    # AI / data / quantum
    "SMCI", "IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "AI", "PATH", "BBAI", "SOUN",
    "VRNT", "DOMO", "CXM", "GTLB",
    # New space / mobility
    "ASTS", "JOBY", "ACHR", "LUNR", "RKLB", "BLDE", "SPCE", "EVTL", "GOEV",
    # Cannabis
    "TLRY", "CGC", "CRON", "ACB", "CURLF", "TCNNF", "GTBIF", "TRUL", "CRLBF", "GRWG",
    # Meme / retail favourites
    "GME", "AMC", "BB", "BBBY", "KOSS", "EXPR", "ATER", "PRTY", "REV",
    # Chinese ADRs
    "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI", "TME", "BILI", "NTES",
    "WB", "TAL", "EDU", "DIDI",
    # EV / auto (small)
    "NKLA", "FSR", "MULN", "FFIE", "LCID",
    # Solar / clean energy
    "ENPH", "FSLR", "RUN", "SHLS", "ARRY", "PLUG", "BLDP", "BE", "FCEL", "BEEM",
    "MAXN", "STEM", "NRGV", "NOVA",
    # Healthcare / biotech (mid)
    "MRNA", "BNTX", "NVAX", "OCGN", "VKTX", "SAVA", "SRPT", "BLUE", "FATE",
    "CRSP", "EDIT", "NTLA", "BEAM", "VRTX", "REGN",
    # Streaming / media (small)
    "ROKU", "SPOT", "FUBO", "SIRI",
    # Sports betting / gaming
    "DKNG", "PENN", "FLUT",
    # Other consumer tech
    "CHWY", "ETSY", "PTON", "BYND", "WBA", "OPEN",
    # Insurance disruptors
    "ROOT", "LMND", "OSCR",
    # Niche tech
    "BAND", "FSLY", "APP", "INTA", "FROG", "CFLT", "GRAB",
    # Semis (small)
    "AEHR", "VECO", "WOLF", "ALGM", "ICHR", "CRDO", "AEIS",
    # Industrial specialty
    "NPO", "ESE", "ROCK",
    # 3D printing
    "DDD", "SSYS", "MTLS", "VLD",
    # Other speculative
    "BBAI", "PHUN", "SPRT", "BFRG", "AXTI", "INDI",
]

SMALL_CAPS_SHARES = [
    # Small biotech
    "TVTX", "AKBA", "VANI", "ANIP", "PRTA", "CRDF", "IOVA", "EDIT", "NTLA",
    "BEAM", "BLUE", "FATE", "ARWR", "HALO", "EXEL", "INSM",
    # Small fintech / consumer
    "OPFI", "PAYO", "ENVA", "WRBY", "FTCH", "REAL", "VRT", "CART", "INST",
    # Small industrials / specialty
    "TILE", "ATR", "GFF", "AEIS", "CRS", "ALSN", "SPXC",
    # Small energy / mining
    "MP", "ALB", "URA", "UEC", "CCJ", "SBSW", "HL", "AG", "EXK", "PAAS",
    "NEM", "GOLD", "HMY", "BTG", "AU", "RGLD", "WPM", "EQX", "FNV",
    "NXE", "DNN", "URG", "LEU",
    # Quantum / AI / sci-fi
    "QUBT", "ARQQ", "INVZ", "OUST", "CGNT", "IRDM",
    # Niche tech
    "DOCN", "BAND", "FSLY", "APP", "INTA", "FROG", "GTLB",
    # Small EV / mobility
    "EVTL", "GOEV", "FSR", "MULN", "ZEV", "NUVB",
    # Small mining / commodities
    "FCEL", "BEEM", "GEVO", "MNTV",
    # Other speculative growth
    "SDIG", "BTBT", "BITF", "HIVE", "GBTC", "PRPL", "AOUT",
    "GENI", "PHUN", "SPRT", "ATER", "REV", "PRTY", "EXPR",
    "BFRG", "NUKK", "AXTI", "INDI", "MTLS",
    # Healthcare / cannabis
    "GTBIF", "TCNNF", "CURLF", "TRUL", "CRLBF", "GRWG",
    # Solar / battery / energy storage small
    "SHLS", "MAXN", "BWXT", "SMR", "OKLO",
    # Misc fintech and software
    "WULF", "BTG", "HUT", "LXRX", "SAVA", "VKTX",
    # Small space / aerospace
    "MNTS", "ASTL", "PLNT", "HLNE", "BMRC",
    # Cybersecurity small
    "S", "TENB", "RPD", "QLYS",
    # Misc growth
    "TWST", "GH", "LRN", "CDLX", "GDOT", "WK",
]

UNIVERSE_OPTIONS = list(dict.fromkeys(LARGE_CAPS + SMALL_MID_CAPS_OPTIONS))
UNIVERSE_SHARES = list(dict.fromkeys(SMALL_MID_CAPS_OPTIONS + SMALL_CAPS_SHARES))
UNIVERSE = list(dict.fromkeys(UNIVERSE_OPTIONS + UNIVERSE_SHARES))

# ---- WSB trending discovery -------------------------------------------
WSB_TRENDING_TOP_N = 30        # extra tickers added at runtime
WSB_TRENDING_MIN_MENTIONS = 3  # threshold to be considered "trending"

# ---- Risk / liquidity floors ------------------------------------------
MIN_OPEN_INTEREST = 100
MIN_DAILY_VOLUME = 25
MAX_BID_ASK_SPREAD_PCT = 0.15
MIN_OPTION_PRICE = 0.10
MIN_DTE = 14
MAX_DTE = 60

# ---- Pricing ----------------------------------------------------------
RISK_FREE_RATE_DEFAULT = 0.045
HESTON_ENABLED = False  # experimental; enable only after a stability validation run

# ---- Sentiment --------------------------------------------------------
SUBREDDITS = ["wallstreetbets", "stocks", "investing", "options", "smallstreetbets"]
SENTIMENT_LOOKBACK_HOURS = 48
SENTIMENT_HALF_LIFE_HOURS = 6.0
USER_AGENT = "optedge-research/0.1 (research@optedge.local)"

# ---- Insider ----------------------------------------------------------
INSIDER_LOOKBACK_DAYS = 90
INSIDER_PRIORITY_TITLES = {"CEO", "CFO", "COO", "PRES", "CHAIRMAN", "DIR", "10%"}
INSIDER_MAX_FILINGS_PER_TICKER = 6    # tight cap = much faster; covers most active names
INSIDER_FAST_MODE = False              # if True, skip XML parsing and use Form 4 counts only

# ---- News (Google News RSS, no key needed) ----------------------------
NEWS_LOOKBACK_DAYS = 7
NEWS_MAX_HEADLINES_PER_TICKER = 30

# ---- Earnings ---------------------------------------------------------
EARNINGS_LOOKAHEAD_DAYS = 30           # how far out to consider for catalyst boosts

# v20.7 — realistic fill cost. Retail option fills are typically 3-8% worse
# than the displayed mid (combined entry+exit half-spread crossings + MM edge).
# Applied to EV and Kelly so the system isn't systematically optimistic.
FILL_SLIPPAGE_PCT = 0.04   # 4% round-trip — adjust per your broker / liquidity

# ---- Concurrency ------------------------------------------------------
# v20.4: bumped where the bottleneck source isn't yfinance anymore.
# - MISPRICING:  6 -> 16 (CBOE primary, doesn't rate-limit)
# - FUNDAMENTALS:8 -> 12 (Yahoo v8 history bypasses yfinance throttle)
# - NEWS:        8 -> 12 (Google News RSS handles concurrency fine)
# - EARNINGS:    6 -> 10
# - VALUE:       8 -> 12
import os as _os
def _wc(default: int, mx: int = 24) -> int:
    """Worker count, optionally scaled to cpu_count for beefy machines."""
    try:
        n = _os.cpu_count() or 4
        return max(default, min(mx, int(n * 1.5)))
    except Exception:
        return default

WORKERS_MISPRICING   = _wc(16)
WORKERS_FUNDAMENTALS = _wc(12)
WORKERS_INSIDER      = 16                   # SEC tolerates ~10 req/sec
WORKERS_NEWS         = _wc(12)
WORKERS_EARNINGS     = _wc(10)
WORKERS_VALUE        = _wc(12)
WORKERS_FUTURES      = 6
WORKERS_CONGRESS     = 1
WORKERS_SOCIAL       = 6
WORKERS_ANALYST      = 6    # Finnhub: 60/min limit, capped here for headroom

# ---- Congress -----------------------------------------------------
CONGRESS_LOOKBACK_DAYS = 90

# ---- Social (StockTwits + Trump Truth Social) ---------------------
SOCIAL_TOP_ST_TICKERS = 30   # how many tickers to query StockTwits for per run
ENGINE_CONCURRENT = True   # run all engines in parallel

# ---- Macro / regime ---------------------------------------------------
VIX_RISK_OFF = 25.0
VIX_RISK_ON = 15.0

# ---- Fusion -----------------------------------------------------------
SIGNAL_WEIGHTS = {
    # v15-v19 base factors (22)
    "mispricing":   0.11,
    "iv_rank":      0.04,
    "skew":         0.03,
    "sentiment_d":  0.08,
    "fundamentals": 0.07,
    "insider":      0.07,
    "macro":        0.06,
    "news":         0.06,
    "earnings":     0.06,
    "value":        0.08,
    "congress":     0.05,
    "social":       0.04,
    "analyst":      0.07,
    "uoa":          0.06,
    "sector_rs":    0.05,
    "dark_pool":    0.04,
    "fda":          0.05,
    "sector_flow":  0.04,
    "technicals":   0.03,
    "short_int":    0.05,
    "put_call":     0.04,
    "iv_surface":   0.04,
    # ---- v20 NEW FACTORS -------------------------------------------------
    # Tier B (10 new free data sources)
    "cot":          0.03,    # CFTC Commitments of Traders weekly mgr-money net change
    "thirteen_f":   0.04,    # SEC 13F smart-money quarter-over-quarter deltas
    "vix_term":     0.03,    # VIX futures term-structure regime (contango/backw'n)
    "eia":          0.02,    # EIA petroleum/natgas weekly inventory surprises
    "wasde":        0.02,    # USDA WASDE monthly ag supply/demand proximity
    "buybacks":     0.04,    # SEC 8-K repurchase program announcements
    "gtrends":      0.02,    # Google search interest momentum (retail attention)
    "form_144":     0.02,    # SEC Form 144 pre-sale notices (bearish-leaning)
    "whisper":      0.02,    # Earningswhispers whisper EPS vs consensus
    "hyperliquid":  0.03,    # Decentralised perpetuals OI + funding (crypto-corr)
    # Tier C (2 new sources + cluster buys post-process on insider)
    "twitter":      0.02,    # Twitter/X cashtag sentiment via Nitter mirrors
    "r_options":    0.02,    # r/options daily-discussion sticky deep scan
    "cluster_buys": 0.03,    # 3+ insiders buying within 14d (proxy: high n_buys)
    # Tier D (3 new risk / portfolio factors)
    "yield_curve":  0.03,    # FRED Treasury curve PCA factors (banks/insurers/duration)
    "credit_spread": 0.03,   # IG/HY OAS divergence (cyclical credit stress)
}

REGIME_FACTOR_MULTIPLIERS = {
    "risk_on": {
        "sentiment_d": 1.25, "social": 1.20, "uoa": 1.20, "sector_rs": 1.15,
        "technicals": 1.15, "short_int": 1.15, "gtrends": 1.15,
        "credit_spread": 0.80, "macro": 0.90,
    },
    "risk_off": {
        "macro": 1.35, "credit_spread": 1.35, "vix_term": 1.25,
        "iv_surface": 1.20, "skew": 1.20, "put_call": 1.15,
        "sentiment_d": 0.75, "social": 0.75, "uoa": 0.80,
        "short_int": 0.80, "gtrends": 0.80,
    },
}

# ---- Analyst (Finnhub) -----------------------------------------------
ANALYST_TOP_N = 80   # query Finnhub for top N tickers

# ---- Value / futures --------------------------------------------------
TOP_N_VALUE = 12        # value plays shown on dashboard
TOP_N_FUTURES = 10      # futures plays shown on dashboard

# ---- Output -----------------------------------------------------------
TOP_N_CALLS = 15
TOP_N_PUTS = 10
TOP_N_SHARES = 15
MAX_PER_TICKER = 1
SHARES_MIN_SCORE = 0.6

# ---- v20 Tier A: Universe pre-filter ---------------------------------
# When full universe exceeds this, slow per-ticker engines run only on
# top-N by market cap + WSB trending + prior signal tickers (preserves coverage
# of new/recent picks without paying for full-universe deep scans every iter).
UNIVERSE_PREFILTER_TOP_N = 300
UNIVERSE_PREFILTER_ENABLED = True

# ---- v20 Tier A: Per-engine SLA timeouts (seconds) -------------------
# Engines exceeding their SLA in concurrent dispatch are abandoned (their
# slot in results becomes empty). Prevents one slow source from blocking
# the whole iteration.
ENGINE_SLA_SECONDS = {
    "mispricing": 240, "fundamentals": 180, "insider": 240,
    "news": 120, "earnings": 120, "value": 180, "futures": 90,
    "congress": 240, "social": 120, "analyst": 120, "sentiment": 90,
    "macro": 60, "sector_rs": 90, "dark_pool": 90, "fda": 45,
    "sector_flow": 45, "technicals": 120, "short_int": 120,
    "cot": 60, "thirteen_f": 240, "vix_term": 30, "eia": 30,
    "wasde": 30, "buybacks": 60, "gtrends": 240, "form_144": 60,
    "whisper": 90, "hyperliquid": 30, "twitter": 120, "r_options": 60,
    "yield_curve": 30, "credit_spread": 30,
}

# ---- v20 Tier D: Risk gates ------------------------------------------
HEDGE_DELTA_THRESHOLD = 5000.0   # $ net delta triggers SPY hedge suggestion
DRAWDOWN_BREAKER_ENABLED = True   # halve Kelly on rolling -10% P&L

ASOF = datetime.now(timezone.utc)
