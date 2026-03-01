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

# Risk Management Rules
MAX_TOTAL_EXPOSURE = 0.30    # Rule 1: 30% max exposure
MAX_OPEN_POSITIONS = 3       # Rule 2: max 3 strikes
MIN_TRADE_USD = 5.0          # Rule 3: $5 minimum

# Book Sanity Rules
MAX_PRICE_DEVIATION = 0.35   # reject if |ask - p_real| > 35c (stale book)
MIN_ASK_LIQUIDITY_USD = 20.0 # reject if ask-side depth < $20 (too thin)

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
        log.info("  Risk: %.0f%% exposure | %d max pos | $%.0f min trade",
                 MAX_TOTAL_EXPOSURE*100, MAX_OPEN_POSITIONS, MIN_TRADE_USD)
        log.info("=" * 60)

        # Discover brackets
        log.info("Discovering Polymarket brackets...")
        if not self.feed.refresh():
            log.warning("No brackets found. Will retry.")

        # Build oracle targets from brackets
        targets = defaultdict(set)
        for br in self.feed.brackets:
            targets[br.end_ts].add(br.strike)
        self.oracle.update_targets(dict(targets))
        log.info("Oracle targets: %d expiries, %d total strikes",
                 len(targets), sum(len(s) for s in targets.values()))

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
        """Main evaluation: Risk checks -> Quant Engine -> Execute."""
        oracle_data = self.oracle.latest
        if not oracle_data:
            return

        brackets = self.feed.get_brackets()
        if not brackets:
            return

        # Get current bids for mark-to-market
        current_bids = {}
        for br in brackets:
            bk = self.feed.get_book(br.yes_tid)
            if bk and bk.bb():
                current_bids[br.strike] = bk.bb()

        # RULE 1: Global Exposure Check (includes pending makers!)
        open_value = self.trader.open_positions_value(current_bids)
        pending_value = getattr(self.trader, 'pending_makers_value', lambda: 0)()
        exposure_ratio = (open_value + pending_value) / self.trader.initial_capital
        if exposure_ratio >= MAX_TOTAL_EXPOSURE:
            return  # silently skip — 70% must stay untouchable

        # RULE 2: Max Positions Check
        open_strikes = self.trader.open_strikes()
        if len(open_strikes) >= MAX_OPEN_POSITIONS:
            return  # already holding max strikes

        # Group brackets by expiry
        groups = defaultdict(list)
        for br in brackets:
            groups[br.end_ts].append(br)

        for end_ts, group_brackets in groups.items():
            exp_data = oracle_data.get(end_ts)
            if not exp_data:
                continue

            spot = exp_data.get("spot_price", 0)
            probs = exp_data.get("probabilities", {})
            if not probs:
                continue

            # Build P(YES) map: only valid strikes
            strike_probs = {}
            entry_prices = {}

            for br in group_brackets:
                prob_data = probs.get(br.strike)
                if not prob_data:
                    continue
                p_yes = prob_data.get("p_real_yes")
                iv = prob_data.get("mark_iv")
                if not _ok(p_yes) or not _ok(iv):
                    continue

                ybk = self.feed.get_book(br.yes_tid)
                if not ybk:
                    continue
                ask = ybk.ba()
                bid = ybk.bb()
                if not ask or ask <= 0:
                    continue

                strike_probs[br.strike] = p_yes
                entry_prices[br.strike] = ask  # worst-case entry = ask

            if not strike_probs:
                continue

            # Run Frank-Wolfe
            try:
                p_states, _ = build_state_probabilities(strike_probs)
                strikes_list = sorted(strike_probs.keys())
                R = build_returns_matrix(strikes_list, entry_prices)
                weights = frank_wolfe_optimizer(p_states, R)
            except Exception as e:
                log.warning("Optimizer: %s", e)
                continue

            # Evaluate each strike
            equity = self.trader.total_equity(current_bids)

            for i, strike in enumerate(strikes_list):
                weight = weights[i] if i < len(weights) else 0
                if weight < 0.001:
                    continue

                # Re-check RULE 2 (may have filled during this loop)
                if len(self.trader.open_strikes()) >= MAX_OPEN_POSITIONS:
                    break

                # Skip if already holding this strike+expiry
                already = any(p.strike == strike and p.end_ts == end_ts
                              for p in self.trader.positions)
                if already:
                    continue

                prob_data = probs.get(strike)
                if not prob_data:
                    continue
                p_real = prob_data["p_real_yes"]

                br = next((b for b in group_brackets if b.strike == strike), None)
                if not br:
                    continue

                ybk = self.feed.get_book(br.yes_tid)
                if not ybk:
                    continue
                best_bid = ybk.bb()
                best_ask = ybk.ba()
                ask_size = ybk.ba_size()

                action, target_price = evaluate_execution(
                    p_real, best_bid, best_ask, MODEL_BUFFER)

                if action == "SKIP_TRADE":
                    continue

                # SANITY: Cross-validate book price vs oracle P(real)
                # If they diverge >35c the book is stale/corrupted
                if abs(target_price - p_real) > MAX_PRICE_DEVIATION:
                    self.trader._add_log(
                        f"SKIP | ${strike:,} | STALE BOOK "
                        f"price={target_price:.4f} p_real={p_real:.4f} "
                        f"diff={abs(target_price-p_real):.3f}")
                    continue

                # SANITY: Minimum ask-side liquidity for takers
                if action == "TAKER_FAK":
                    ask_liq_usd = (ask_size or 0) * (best_ask or 0)
                    if ask_liq_usd < MIN_ASK_LIQUIDITY_USD:
                        self.trader._add_log(
                            f"SKIP | ${strike:,} | THIN BOOK "
                            f"depth=${ask_liq_usd:.1f} < ${MIN_ASK_LIQUIDITY_USD}")
                        continue

                allocation_usd = equity * weight
                allocation_usd = min(allocation_usd, self.trader.capital * 0.3)

                # Liquidity cap for takers
                if action == "TAKER_FAK" and best_ask:
                    max_liq = ask_size * best_ask
                    allocation_usd = min(allocation_usd, max_liq)

                # RULE 3: Minimum trade size
                if allocation_usd < MIN_TRADE_USD:
                    continue

                # Log signal
                edge = (p_real - best_ask) if best_ask else 0
                self.trader._add_log(
                    f"SIGNAL | ${strike:,} | edge={edge:.4f} | "
                    f"w={weight:.3f} | ${allocation_usd:.2f} | {action}")

                # RULE 4: Execute under lock
                entry_reason = f"edge={edge*100:.1f}% w={weight:.3f} iv={prob_data.get('mark_iv',0)*100:.1f}%"
                if action == "TAKER_FAK":
                    await self.trader.submit_taker(
                        strike, target_price, allocation_usd, end_ts,
                        event_title=br.event_title, entry_reason=entry_reason,
                        edge=edge, book=ybk)
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

            exp_data = oracle_data.get(br.end_ts)
            if not exp_data:
                continue

            prob_data = exp_data.get("probabilities", {}).get(pos.strike)
            if not prob_data:
                continue

            p_real = prob_data["p_real_yes"]
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
