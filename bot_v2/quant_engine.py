"""
quant_engine.py - Pure stateless math module.
4-Phase Pipeline from The Master Quant Architecture.

Phase 1: Black-Scholes N(d2) probability extraction
Phase 2: Frank-Wolfe Kelly portfolio optimisation (Cash Asset)
Phase 3: Hybrid execution (Taker / Maker / Skip)
Phase 4: Smart TP lifecycle management
"""

import math
import numpy as np
from scipy.stats import norm


# =====================================================================
# PHASE 1 - Probability Extraction
# =====================================================================

def calculate_nd2(spot, strike, t_years, iv, r=0.0):
    """Extract risk-neutral probability P(S > K) = N(d2)."""
    if not _ok(spot) or not _ok(strike) or not _ok(iv) or not _ok(t_years):
        return float("nan")
    if t_years <= 0 or iv <= 0:
        return 1.0 if spot >= strike else 0.0
    if strike <= 0 or spot <= 0:
        return float("nan")
    sqrt_t = np.sqrt(t_years)
    d1 = (np.log(spot / strike) + (r + (iv ** 2) / 2) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return float(norm.cdf(d2))


def _ok(v):
    """Check value is not None/NaN."""
    if v is None:
        return False
    try:
        return not math.isnan(float(v))
    except (ValueError, TypeError):
        return False


# =====================================================================
# PHASE 2 - Frank-Wolfe Kelly Optimisation
# =====================================================================

def build_state_probabilities(strike_probs):
    """Build discrete state probability vector. N strikes -> N+1 states."""
    strikes = sorted(strike_probs.keys())
    if not strikes:
        return np.array([1.0]), ["empty"]
    n = len(strikes)
    probs, labels = [], []
    probs.append(1.0 - strike_probs[strikes[0]])
    labels.append(f"S<{strikes[0]}")
    for i in range(1, n):
        p = strike_probs[strikes[i - 1]] - strike_probs[strikes[i]]
        probs.append(max(p, 1e-9))
        labels.append(f"{strikes[i-1]}<S<{strikes[i]}")
    probs.append(strike_probs[strikes[-1]])
    labels.append(f"S>{strikes[-1]}")
    p_states = np.array(probs, dtype=np.float64)
    p_states = np.clip(p_states, 1e-9, None)
    p_states /= p_states.sum()
    return p_states, labels


def build_returns_matrix(strikes, entry_prices):
    """R[state, asset]: Win = (1/price)-1, Lose = -1. YES wins when S>K."""
    ss = sorted(strikes)
    n_st, n_as = len(ss) + 1, len(ss)
    R = np.full((n_st, n_as), -1.0, dtype=np.float64)
    for j, k in enumerate(ss):
        p = entry_prices.get(k, 0.5)
        if p <= 0: p = 0.01
        wr = (1.0 / p) - 1.0
        for s in range(j + 1, n_st):
            R[s, j] = wr
    return R


def frank_wolfe_optimizer(p_states, R_matrix, max_iter=5000, tol=1e-6):
    """Frank-Wolfe Kelly with Cash Asset. Returns half-Kelly weights."""
    ns, na = R_matrix.shape
    R_ext = np.hstack([R_matrix, np.zeros((ns, 1))])
    w = np.zeros(na + 1)
    w[-1] = 1.0
    for k in range(max_iter):
        wealth = np.clip(1.0 + R_ext @ w, 1e-12, None)
        grad = (p_states / wealth) @ R_ext
        best = int(np.argmax(grad))
        if np.max(grad) - grad @ w < tol:
            break
        s = np.zeros(na + 1)
        s[best] = 1.0
        gamma = 2.0 / (k + 2.0)
        w = w + gamma * (s - w)
    return w[:-1] * 0.5


# =====================================================================
# PHASE 3 - Hybrid Execution Logic
# =====================================================================

def evaluate_execution(p_real, best_bid, best_ask, model_buffer=0.02):
    """Returns (action, target_price)."""
    if not _ok(p_real) or not _ok(best_bid) or not _ok(best_ask):
        return ("SKIP_TRADE", 0.0)
    if best_ask <= 0 or best_bid <= 0:
        return ("SKIP_TRADE", 0.0)
    spread = max(0.0, best_ask - best_bid)
    dyn_thresh = spread + model_buffer
    if p_real - best_ask >= dyn_thresh:
        return ("TAKER_FAK", best_ask)
    elif p_real - (best_bid + 0.001) >= model_buffer:
        return ("MAKER_POST_ONLY", best_bid + 0.001)
    else:
        return ("SKIP_TRADE", 0.0)


# =====================================================================
# PHASE 4 - Smart Take-Profit
# =====================================================================

def evaluate_exit(p_real, best_bid, bid_size, tokens_held, days_remaining,
                  entry_price, time_discount_rate=0.01):
    """Returns (action, optimal_tp, reason)."""
    optimal_tp = p_real - (days_remaining * time_discount_rate)
    if not _ok(best_bid) or best_bid <= 0:
        return ("HOLD", optimal_tp, "no_bid")
    if best_bid < entry_price:
        return ("HOLD", optimal_tp, "below_entry")
    if best_bid >= optimal_tp and bid_size >= tokens_held:
        return ("MARKET_SELL", optimal_tp, "tp_hit_liquid")
    elif best_bid >= optimal_tp:
        return ("HOLD", optimal_tp, "tp_hit_thin_book")
    else:
        return ("HOLD", optimal_tp, "below_tp")
