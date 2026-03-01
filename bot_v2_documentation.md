# Polymarket Quant Arbitrage Bot v2 — Documentation

This document serves as the complete technical manual for the `bot_v2` system. It explains the architecture, the role of every Python file, the mathematical logic, and the safety rules that govern the bot's behavior.

---

## 1. System Architecture overview

The system is built as a modular, asynchronous High-Frequency Trading (HFT) orchestrator. It connects to live data feeds (Polymarket WebSocket, Binance WebSocket, Deribit REST), processes the data through a stateless quantitative engine, manages simulated capital and positions, and exposes a live web dashboard.

**The system consists of exactly 5 Python files:**

1.  **`feeds.py`:** The Sensory Layer (Data Ingestion)
2.  **`quant_engine.py`:** The Brain (Math & Logic)
3.  **`paper_trader.py`:** The Exchange Simulator (Capital & Portfolio)
4.  **`bot.py`:** The Orchestrator (Risk & Execution)
5.  **`dashboard.py`:** The UI (Visualization)

---

## 2. File Roles & Responsibilities

### `feeds.py`
Connects to external APIs and normalizes data into standard Python objects (`Book`, `Bracket`).
*   **`PolymarketFeed`:** Discovers "Bitcoin above $X" brackets via REST, then opens WebSockets to stream live order books (Bids/Asks) for all discovered YES tokens.
*   **`BinanceFeed`:** Streams the live BTC/USDT price via WebSocket.
*   **`DeribitOracle`:** Polls Deribit's BTC options (Calls and Puts) every 5 seconds. It calculates the Implied Volatility (IV) for specific strikes and expiry dates to determine the "fair value" of the Polymarket brackets.
*   *Note:* Implements a custom DNS resolver fix for Windows `aiohttp` to prevent connection drops.

### `quant_engine.py`
A pure, stateless module containing the core mathematical pipeline. It takes raw data and outputs trading decisions. It contains 4 phases:
1.  **Phase 1 Extract Probability:** `calculate_nd2()`. Uses the Black-Scholes $N(d2)$ formula to convert Deribit IV and Time-to-Expiry into $P_{real}$ (the true probability BTC will be above the strike).
2.  **Phase 2 Sizing:** `frank_wolfe_optimizer()`. Uses the Kelly Criterion across multiple strikes simultaneously to calculate the optimal percentage of capital to risk. Includes a "Cash Asset" (0% return vector) to prevent 100% all-in behavior, and applies a Half-Kelly fraction to smooth drawdowns.
3.  **Phase 3 Execution:** `evaluate_execution()`. Compares $P_{real}$ against the live order book spread. Decides whether to aggressively cross the spread (`TAKER_FAK`), queue passively at the bid (`MAKER_POST_ONLY`), or `SKIP_TRADE`.
4.  **Phase 4 Exit:** `evaluate_exit()`. Calculates an `optimal_tp` (Take Profit) that decays linearly over time (Smart TP). If the live bid crosses this threshold, it outputs `MARKET_SELL`.

### `paper_trader.py`
Simulates the actual exchange. It manages the virtual $1,000 capital, tracks open/closed positions, logs trades to CSV, and calculates mark-to-market PnL.
*   **Taker Fills:** Instant guaranteed execution, deducting capital immediately.
*   **Maker Fills:** Simulates queueing an order. It waits 5-30 seconds, applies a 60% probability of fill, and *crucially*, re-validates the order book at fill time. If the market has moved away by >20%, the fill is canceled to prevent unrealistic PnL.
*   **Safety Checks:** Rejects obviously broken order books (e.g., bids < $0.01 or data older than 30 seconds).

### `bot.py`
The master controller that ties everything together. It runs an infinite async loop every 5 seconds.
*   **The Loop:** Updates Feeds → Checks Risk Rules → Calls Quant Engine → Calls Paper Trader.
*   **Risk Management (The Defense Shield):**
    *   **Rule 1 (Exposure):** Will not open new trades if 30% or more of the capital is currently invested.
    *   **Rule 2 (Max Strikes):** Will hold a maximum of 3 different strikes concurrently.
    *   **Rule 3 (Dust Filter):** Rejects allocations smaller than $5.00.
    *   **Rule 4 (Concurrency Lock):** Uses `asyncio.Lock()` to ensure capital is deducted synchronously before the next trade calculates its Kelly size.
    *   **Anti-Stale Guard:** Cross-validates the Polymarket orderbook against the Oracle $P_{real}$. If they differ by >35 cents, it rejects the trade as a "Stale Book" anomaly.
*   **Anti-Zombie:** Registers an `atexit` handler to guarantee all processes (including the web server) die cleanly on `Ctrl+C`.

### `dashboard.py`
A Flask web server running on `http://127.0.0.1:5555`.
*   Uses embedded TailwindCSS for a dark-themed UI.
*   Provides REST APIs (`/api/stats`, `/api/positions`, `/api/history`) that the frontend JavaScript polls every 2 seconds.
*   Displays KPIs (Capital, Win Rate), Open Positions with live mark-to-market PnL, Market Brackets (with calculated Edge%), and a unified Trade/Log history.

---

## 3. The Lifecycle of a Trade

1.  **Discovery:** `feeds.py` finds "Bitcoin above $70,000 on March 1?" and connects to its orderbook.
2.  **Valuation:** `feeds.py` (Oracle) pulls Deribit IV for $70k calls on March 1.
3.  **Evaluation:** Every 5 seconds, `bot.py` passes the Polymarket Book and Oracle IV into `quant_engine.py`.
4.  **Math Pipeline:**
    *   *Phase 1:* N(d2) says true probability ($P_{real}$) is 0.45 ($0.45).
    *   *Phase 3:* The live ask is $0.40. Edge = +0.05. The engine signals `TAKER_FAK`.
    *   *Phase 2:* Frank-Wolfe calculates we should risk 2% of our equity based on that edge.
5.  **Risk Check:** `bot.py` checks Rule 1 (Exposure < 30%), Rule 2 (Positions < 3), and Rule 3 (Alloc > $5). It also checks the Anti-Stale guard (is difference between 0.40 and 0.45 > 0.35? No).
6.  **Execution:** `bot.py` calls `paper_trader.py`'s `submit_taker()`.
7.  **Logging:** `paper_trader.py` deducts $20 (2% of $1000) from virtual capital, creates a `Position` object storing the entry price ($0.40), and writes an `OPEN` row to `trade_history.csv`.
8.  **Exit Evaluation:** On subsequent loops, `bot.py` passes the open position to Phase 4 (Smart TP). The target profit drifts lower as expiry approaches. When the live bid crosses the target, it signals `MARKET_SELL`.
9.  **Close:** `paper_trader.py` calculates profit, adds proceeds back to capital, marks position CLOSED, and appends a `CLOSE` row to the CSV.
