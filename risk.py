"""
risk.py — hard gates, evaluated AFTER the gatekeeper's decision.

Design rule for the write-up: the LLM may veto or shrink, never approve past
a gate and never upsize. Every gate below is deterministic and auditable.

    decision = gatekeeper(brief)          # may veto, may set size_mult <= 1.0
    ok, reason = GATES.check_entry(...)   # may still refuse
    if decision.approved and ok: place()
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import List, Optional, Tuple


@dataclass
class RiskConfig:
    equity_start: float = 100_000.0

    # Sizing
    risk_per_trade_pct: float = 0.005      # 0.5% of starting equity at risk
    max_total_risk_pct: float = 0.03       # 3% deployed at any one time
    max_contracts_per_spread: int = 10

    # Concentration
    max_open_positions: int = 4
    max_positions_per_underlying: int = 2
    allowed_underlyings: tuple = ("SPY", "QQQ", "IWM")

    # Loss control
    daily_loss_limit_pct: float = 0.01     # stop entering for the day
    kill_switch_dd_pct: float = 0.025      # stop entering for the whole event

    # Quality floors (belt and braces over candidates.py)
    min_credit_pct_width: float = 0.18
    max_short_delta: float = 0.18
    min_dte: int = 0
    max_dte: int = 3

    # Competition calendar (UTC)
    last_entry_utc: datetime = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
    force_flat_utc: datetime = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)
    no_entry_before: time = time(14, 45)   # let the open settle
    no_entry_after: time = time(19, 45)    # avoid the closing scramble


@dataclass
class PortfolioState:
    equity: float
    day_start_equity: float
    open_positions: List[dict] = field(default_factory=list)

    @property
    def total_risk_open(self) -> float:
        return sum(p.get("max_loss", 0.0) for p in self.open_positions)

    @property
    def day_pnl_pct(self) -> float:
        return (self.equity - self.day_start_equity) / max(self.day_start_equity, 1)

    def count_for(self, underlying: str) -> int:
        return sum(1 for p in self.open_positions if p.get("underlying") == underlying)


class RiskGates:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    # ---- sizing -------------------------------------------------------
    def size(self, spread, state: PortfolioState, llm_mult: float = 1.0) -> int:
        """Contracts to trade. llm_mult is clamped to [0, 1] — never upsizes."""
        c = self.cfg
        mult = max(0.0, min(1.0, float(llm_mult)))
        budget = c.equity_start * c.risk_per_trade_pct * mult
        per_contract = spread["max_loss"]
        if per_contract <= 0:
            return 0
        n = int(budget // per_contract)

        headroom = c.equity_start * c.max_total_risk_pct - state.total_risk_open
        n = min(n, int(max(headroom, 0) // per_contract), c.max_contracts_per_spread)
        return max(n, 0)

    # ---- entry gates --------------------------------------------------
    def check_entry(self, spread: dict, state: PortfolioState,
                    now: Optional[datetime] = None) -> Tuple[bool, str]:
        c = self.cfg
        now = now or datetime.now(timezone.utc)

        if state.equity <= c.equity_start * (1 - c.kill_switch_dd_pct):
            return False, "kill switch: event drawdown limit hit"
        if state.day_pnl_pct <= -c.daily_loss_limit_pct:
            return False, "daily loss limit hit"
        if now >= c.last_entry_utc:
            return False, "past final entry time for the event"
        if not (c.no_entry_before <= now.time() <= c.no_entry_after):
            return False, "outside the entry window"

        if spread["underlying"] not in c.allowed_underlyings:
            return False, f"{spread['underlying']} not on the allowed list"
        if len(state.open_positions) >= c.max_open_positions:
            return False, "max open positions"
        if state.count_for(spread["underlying"]) >= c.max_positions_per_underlying:
            return False, "max positions for this underlying"

        if spread["credit_pct_width"] < c.min_credit_pct_width:
            return False, "credit below floor"
        if spread["short_delta"] > c.max_short_delta:
            return False, "short delta too high"
        if not (c.min_dte <= spread["dte"] <= c.max_dte):
            return False, "expiry outside window"

        # Expiry must land before the submission cut, or P&L is unrealised.
        exp = datetime.fromisoformat(spread["expiry"]).replace(tzinfo=timezone.utc)
        if exp > c.force_flat_utc:
            return False, "expires after the deadline"

        if self.size(spread, state) < 1:
            return False, "no risk headroom for even one contract"

        return True, "ok"

    # ---- exit gates ---------------------------------------------------
    def check_exit(self, position: dict, state: PortfolioState,
                   now: Optional[datetime] = None) -> Tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if now >= self.cfg.force_flat_utc:
            return True, "force flat before submission"
        if position.get("unrealised", 0) >= 0.60 * position.get("credit_total", 1e9):
            return True, "profit target: 60% of credit captured"
        if position.get("unrealised", 0) <= -1.5 * position.get("credit_total", 0):
            return True, "stop: loss reached 1.5x credit"
        if position.get("short_delta_now", 0) >= 0.35:
            return True, "short strike under threat"
        return False, "hold"


GATES = RiskGates(RiskConfig())
