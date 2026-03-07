"""
bot.py - The Orchestrator.
Connects feeds, quant engine, paper trader, and dashboard.

Risk Rules (from Master Quant Architecture):
  Rule 1: 30% max exposure
  Rule 2: Max 3 concurrent positions
  Rule 3: $5 minimum trade size
  Rule 4: asyncio.Lock for sequential trade execution
"""

import atexit
import asyncio
import logging
import os
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

from feeds import DeribitOracle, PolymarketFeed
from quant_engine import (
    calculate_nd2, build_state_probabilities, build_returns_matrix,
    frank_wolfe_optimizer, evaluate_execution, evaluate_exit, _ok,
    get_position_cap,
)
from config import (
    MAX_GLOBAL_EXPOSURE, MAX_EXPOSURE_PER_DATE, KELLY_FRACTION,
    MIN_EXECUTION_WEIGHT, MIN_TRADE_USD as CFG_MIN_TRADE,
    MAX_PRICE_DEVIATION as CFG_PRICE_DEV,
    MIN_ASK_LIQUIDITY_USD as CFG_MIN_LIQ,
)
from paper_trader import PaperTrader
from dashboard import create_app

# ===================== CONFIGURATION =================================
MODEL_BUFFER = 0.02          # 2% IV/drift tolerance
TIME_DISCOUNT_RATE = 0.01   # 1% EV/day for Smart TP
STRATEGY_LOOP_SEC = 5
ORACLE_POLL_SEC = 5
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5555

# Risk Management Rules — values sourced from config.py (3D Risk Matrix)
# MAX_GLOBAL_EXPOSURE    = 0.30  (from config)
# MAX_EXPOSURE_PER_DATE  = 0.15  (from config)
# KELLY_FRACTION         = 0.15  (from config)
# Probability-tiered caps: 5% / 3% / 1.5% via get_position_cap()
MIN_TRADE_USD         = CFG_MIN_TRADE     # $5 minimum
MAX_PRICE_DEVIATION   = CFG_PRICE_DEV     # 35c stale-book guard
MIN_ASK_LIQUIDITY_USD = CFG_MIN_LIQ       # $20 minimum ask depth

# ===================== LOGGING =======================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# ===================== THE ORCHESTRATOR ==============================

class TradingBot:
    def __init__(self):
        self.feed = PolymarketFeed()
        self.oracle = DeribitOracle()
        self.trader = PaperTrader()
        self.trade_lock = self.trader.trade_lock  # Rule 4
        self.status = "INIT"
        self.start_time = time.time()

    async def run(self):
        log.info("=" * 60)
        log.info("  QUANT ARB ENGINE — Paper Trading v2")
        log.info("  Capital: $%.2f | Buffer: %.1f%% | Discount: %.1f%%/day",
                 self.trader.initial_capital, MODEL_BUFFER*100, TIME_DISCOUNT_RATE*100)
        log.info("  Risk: %.0f%% global | %.0f%% per-date | Kelly=%.0f%% | $%.0f min",
                 MAX_GLOBAL_EXPOSURE*100, MAX_EXPOSURE_PER_DATE*100,
                 KELLY_FRACTION*100, MIN_TRADE_USD)
        log.info("=" * 60)

        log.info("Starting feeds and engines...")

        # Start dashboard in daemon thread
        app = create_app(self)
        dash_thread = threading.Thread(
            target=lambda: app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT,
                                   debug=False, use_reloader=False),
            daemon=True)
        dash_thread.start()
        log.info("Dashboard: http://%s:%d", DASHBOARD_HOST, DASHBOARD_PORT)

        self.status = "RUNNING"

        # Run all async loops
        await asyncio.gather(
            self.feed.run(),
            self.oracle.run(poll_sec=ORACLE_POLL_SEC),
            self._strategy_loop(),
            self._maker_loop(),
        )

    # === STRATEGY LOOP ===============================================

    async def _strategy_loop(self):
        await asyncio.sleep(12)  # let feeds warm up
        log.info("Strategy loop starting (every %ds)", STRATEGY_LOOP_SEC)

        while True:
            try:
                # Dynamically update oracle targets based on latest discovered brackets
                targets = defaultdict(set)
                for br in self.feed.brackets:
                    targets[br.end_ts].add(br.strike)
                self.oracle.update_targets(dict(targets))

                await self._resolve_expired()
                await self._evaluate()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Strategy tick failed")
            await asyncio.sleep(STRATEGY_LOOP_SEC)

    async def _resolve_expired(self):
        """Resolve positions whose bracket has expired."""
        now = time.time()
        btc = self.feed.get_btc_price()
        if not btc:
            return
        for pos in list(self.trader.open_positions()):
            if pos.end_ts > 0 and pos.end_ts < now:
                await self.trader.resolve_position(pos, btc)

    async def _evaluate(self):
        """Main evaluation: Risk checks -> Per-Bracket IV -> Quant Engine -> Execute.

        STRICT PER-BRACKET T_YEARS AND IV:
          t_years = (br.end_ts - now) / SECS            (this bracket's exact time)
          iv      = oracle.get_iv_for_date(target_dt, strike) (this bracket's Deribit IV)
          p_real  = calculate_nd2(spot, strike, t_years, iv)  (this bracket's probability)
        """
        spot = self.oracle.spot_price
        if not spot or spot <= 0:
            return

        brackets = self.feed.get_brackets()
        if not brackets:
            return

        now_ts = datetime.now(timezone.utc).timestamp()

        # current bids for mark-to-market
        current_bids = {}
        for br in brackets:
            bk = self.feed.get_book(br.yes_tid)
            if bk and bk.bb():
                current_bids[(br.strike, br.end_ts)] = bk.bb()

        # RULE 1: Global Exposure Check (Dimension 1)
        open_value    = self.trader.open_positions_value(current_bids)
        pending_value = self.trader.pending_makers_value()
        if (open_value + pending_value) / self.trader.initial_capital >= MAX_GLOBAL_EXPOSURE:
            return

        equity = self.trader.total_equity(current_bids)

        # PASS 1: compute t_years, iv, p_real INDIVIDUALLY per bracket
        bd = {}  # (strike, end_ts) -> {p_real, iv, t_years, exp_code, br}
        for br in brackets:
            if br.end_ts <= now_ts:
                continue
            t_years = (br.end_ts - now_ts) / 31_536_000.0
            if t_years <= 0:
                continue
            target_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            iv, exp_code = self.oracle.get_iv_for_date(target_dt, br.strike)
            if iv is None or iv <= 0:
                continue
            p_real = calculate_nd2(spot, br.strike, t_years, iv)
            if not _ok(p_real):
                continue
            bd[(br.strike, br.end_ts)] = dict(
                p_real=p_real, iv=iv, t_years=t_years, exp_code=exp_code, br=br)

        if not bd:
            return

        # PASS 2: group by end_ts for Frank-Wolfe
        groups = defaultdict(list)
        for (strike, end_ts), info in bd.items():
            groups[end_ts].append((strike, info))

        for end_ts, items in groups.items():
            strike_probs = {}
            entry_prices = {}
            info_map     = {}

            for strike, info in items:
                br  = info["br"]
                ybk = self.feed.get_book(br.yes_tid)
                if not ybk: continue
                ask = ybk.ba()
                if not ask or ask <= 0: continue
                strike_probs[strike] = info["p_real"]
                entry_prices[strike] = ask
                info_map[strike]     = info

            if not strike_probs:
                continue

            try:
                p_states, _  = build_state_probabilities(strike_probs)
                strikes_list = sorted(strike_probs.keys())
                R            = build_returns_matrix(strikes_list, entry_prices)
                weights      = frank_wolfe_optimizer(p_states, R)
            except Exception as e:
                log.warning("Optimizer: %s", e)
                continue

            for i, strike in enumerate(strikes_list):
                weight = weights[i] if i < len(weights) else 0
                if weight < 0.001:
                    continue

                # Skip if already holding OR pending for this strike+expiry
                already_open = any(
                    p.strike == strike and p.end_ts == end_ts
                    for p in self.trader.positions
                )
                already_pending = any(
                    o["strike"] == strike and o["end_ts"] == end_ts
                    for o in self.trader.pending_makers
                )
                if already_open or already_pending:
                    continue

                info   = info_map.get(strike)
                if not info: continue
                p_real = info["p_real"]
                iv     = info["iv"]
                br     = info["br"]

                ybk = self.feed.get_book(br.yes_tid)
                if not ybk: continue
                best_bid = ybk.bb()
                best_ask = ybk.ba()
                ask_size = ybk.ba_size()

                action, target_price, avail_size = evaluate_execution(
                    p_real, best_bid, best_ask, MODEL_BUFFER, ask_size)
                if action == "SKIP_TRADE":
                    continue

                if abs(target_price - p_real) > MAX_PRICE_DEVIATION:
                    self.trader._add_log(
                        f"SKIP | ${strike:,} | STALE BOOK "
                        f"price={target_price:.4f} p_real={p_real:.4f} "
                        f"diff={abs(target_price-p_real):.3f}")
                    continue

                if action == "TAKER_FAK":
                    ask_liq = (ask_size or 0) * (best_ask or 0)
                    if ask_liq < MIN_ASK_LIQUIDITY_USD:
                        self.trader._add_log(
                            f"SKIP | ${strike:,} | THIN BOOK depth=${ask_liq:.1f}")
                        continue

                # ── 3D RISK MATRIX SIZING ────────────────────────────────────
                initial_cap = self.trader.initial_capital

                # Step A: Fractional Kelly de-leveraging
                adjusted_kelly = weight * KELLY_FRACTION

                # Step B: Probability-tiered cap
                tier_cap      = get_position_cap(p_real)
                target_weight = min(adjusted_kelly, tier_cap)

                # Step C: Portfolio gates
                # Gate 1 — global exposure remaining
                current_global = (open_value + pending_value) / initial_cap
                allowed_global = max(0.0, MAX_GLOBAL_EXPOSURE - current_global)

                # Gate 2 — per-date correlation defence
                target_date = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
                date_value  = sum(
                    pos.cost_usd for pos in self.trader.open_positions()
                    if datetime.fromtimestamp(pos.end_ts, tz=timezone.utc).date() == target_date
                ) + sum(
                    o["allocation_usd"] for o in self.trader.pending_makers
                    if datetime.fromtimestamp(o["end_ts"], tz=timezone.utc).date() == target_date
                )
                current_date = date_value / initial_cap
                allowed_date = max(0.0, MAX_EXPOSURE_PER_DATE - current_date)

                # Final clamped weight
                final_weight = min(target_weight, allowed_global, allowed_date)

                if final_weight <= MIN_EXECUTION_WEIGHT:
                    continue   # dust — not worth the API call

                allocation_usd = equity * final_weight

                # Hard liquidity cap for takers
                if action == "TAKER_FAK" and best_ask:
                    allocation_usd = min(allocation_usd, ask_size * best_ask)

                if allocation_usd < MIN_TRADE_USD:
                    continue
                # ── END 3D RISK MATRIX ────────────────────────────────────────

                edge = (p_real - best_ask) if best_ask else 0
                self.trader._add_log(
                    f"SIGNAL | ${strike:,} | edge={edge:.4f} | "
                    f"kelly={weight:.3f}→{final_weight:.3f} | "
                    f"${allocation_usd:.2f} | {action} | "
                    f"T={info['t_years']*365:.1f}d iv={iv*100:.1f}% "
                    f"[{info['exp_code']}] tier={tier_cap*100:.1f}%")

                entry_reason = (f"edge={edge*100:.1f}% kelly={weight:.3f}→{final_weight:.3f} "
                                f"iv={iv*100:.1f}% T={info['t_years']*365:.1f}d "
                                f"deribit={info['exp_code']} tier={tier_cap*100:.1f}%")
                if action == "TAKER_FAK":
                    await self.trader.submit_taker(
                        strike, target_price, allocation_usd, end_ts,
                        event_title=br.event_title, entry_reason=entry_reason,
                        edge=edge, book=ybk, ask_size=avail_size)
                elif action == "MAKER_POST_ONLY":
                    await self.trader.submit_maker(
                        strike, target_price, allocation_usd, end_ts,
                        event_title=br.event_title, entry_reason=entry_reason,
                        edge=edge, book=ybk, yes_tid=br.yes_tid)

        # --- Phase 4: Lifecycle (Smart TP) ---
        now = datetime.now(timezone.utc)
        for pos in self.trader.open_positions():
            # Match bracket by strike + end_ts
            br = next((b for b in brackets
                       if b.strike == pos.strike
                       and abs(b.end_ts - pos.end_ts) < 60), None)
            if not br:
                continue

            # Per-bracket p_real for Smart TP
            target_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            now_ts_tp = datetime.now(timezone.utc).timestamp()
            t_years_tp = max(0, (br.end_ts - now_ts_tp) / 31_536_000.0)
            iv_tp, _ = self.oracle.get_iv_for_date(target_dt, pos.strike)
            if iv_tp is None or iv_tp <= 0:
                continue
            spot_tp = self.oracle.spot_price
            if not spot_tp or spot_tp <= 0:
                continue
            p_real = calculate_nd2(spot_tp, pos.strike, t_years_tp, iv_tp)
            if not _ok(p_real):
                continue
            end_dt = datetime.fromtimestamp(br.end_ts, tz=timezone.utc)
            days_remaining = max(0, (end_dt - now).total_seconds() / 86400)

            ybk = self.feed.get_book(br.yes_tid)
            if not ybk:
                continue
            best_bid = ybk.bb()
            bid_size = ybk.bb_size()

            exit_action, optimal_tp, reason = evaluate_exit(
                p_real, best_bid, bid_size,
                pos.tokens, days_remaining,
                pos.entry_price, TIME_DISCOUNT_RATE)

            if exit_action == "MARKET_SELL" and best_bid:
                await self.trader.exit_position(pos, best_bid, f"TP:{reason}")

    # === MAKER LOOP ==================================================

    async def _maker_loop(self):
        while True:
            try:
                await self.trader.process_pending_makers(
                    get_book=self.feed.get_book)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("Maker loop error")
            await asyncio.sleep(1)

    # === SHUTDOWN =====================================================

    def stop(self):
        if self.status == "STOPPED":
            return
        self.feed.stop()
        self.oracle.stop()
        self.status = "STOPPED"
        log.info("Bot stopped.")


# ===================== ANTI-ZOMBIE ENTRY POINT =======================

_bot = None

def _on_exit():
    """Guaranteed cleanup: stop bot + force kill Flask thread."""
    global _bot
    if _bot and _bot.status != "STOPPED":
        _bot.stop()
    print("\nBot stopped cleanly.")
    os._exit(0)

atexit.register(_on_exit)


async def main():
    global _bot
    _bot = TradingBot()
    try:
        await _bot.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if _bot.status != "STOPPED":
            _bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _on_exit()
