"""
config.py - Centralised risk constants for the Quant Arb Engine.

3D Risk Matrix dimensions:
  Dim 1: MAX_GLOBAL_EXPOSURE    — total portfolio heat ceiling
  Dim 2: MAX_EXPOSURE_PER_DATE  — correlation defence per expiry date
  Dim 3: KELLY_FRACTION         — fractional Kelly de-leveraging
"""

# ── Dimension 1: Global Safety ─────────────────────────────────────────
MAX_GLOBAL_EXPOSURE   = 0.30   # 30% of initial capital max in-market at once

# ── Dimension 2: Time-Correlation Defence ─────────────────────────────
MAX_EXPOSURE_PER_DATE = 0.15   # 15% max across all positions sharing same expiry date

# ── Dimension 3: Fractional Kelly Multiplier ──────────────────────────
KELLY_FRACTION        = 0.15   # use 15% of raw Kelly output (conservative half-Kelly)

# ── Probability-Tiered Position Caps ──────────────────────────────────
# Applied AFTER Kelly fraction, BEFORE portfolio gates
TIER_HIGH_PROB_THRESH    = 0.80   # p_real > 80%  → deep ITM, high confidence
TIER_STANDARD_PROB_THRESH = 0.40  # p_real > 40%  → standard
TIER_HIGH_CAP            = 0.050  # 5.0% of initial capital
TIER_STANDARD_CAP        = 0.030  # 3.0% of initial capital
TIER_LOTTO_CAP           = 0.015  # 1.5% of initial capital (deep OTM)

# ── Dust Threshold ────────────────────────────────────────────────────
MIN_EXECUTION_WEIGHT = 0.005   # skip if final weight < 0.5% (not worth it)

# ── Other risk params (kept here for single source of truth) ──────────
MIN_TRADE_USD          = 5.0
MAX_PRICE_DEVIATION    = 0.35
MIN_ASK_LIQUIDITY_USD  = 20.0
MODEL_BUFFER           = 0.02
TIME_DISCOUNT_RATE     = 0.01
