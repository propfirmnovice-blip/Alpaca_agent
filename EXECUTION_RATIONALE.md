# Execution design — justification for the write-up

Notes to support the one-page submission. Each section states the choice, the
reason, and the mechanism in code, so the write-up can cite an enforced rule
rather than an intention.

---

## 1. Why a credit floor derived from history, not from the chain

The naive selection rule is "take the best credit available at 12 delta."
That rule has no reference point: it accepts whatever the market offers on the
day, which means it trades hardest precisely when premium is thin.

Instead the floor is derived empirically. A put credit spread's expiry payoff
depends only on the underlying's forward return and the credit taken, so the
entire payoff distribution can be reconstructed from years of daily bars
without any historical options data. `validate_spread.py` does this and reports
the **breakeven credit** `c*` — the credit at which expectancy is exactly zero —
including the worst individual year in the sample.

The agent then requires:

    credit >= width * worst_year_breakeven * 1.5

Two things follow. The strategy is calibrated to its worst historical year
rather than its average, and there is a defined state — "the chain isn't paying
enough" — in which the correct action is to place no trade at all. Enforced in
`executor.credit_ok()`.

**Write-up line:** entry pricing is anchored to an empirically measured
breakeven, with a 1.5x margin against the worst year in an eight-year sample.

---

## 2. Why implied vol is checked against realised vol

Selling a credit spread is selling insurance. It is profitable when the
insurance is priced above the risk being insured. The cleanest available proxy
is the short leg's implied volatility against the underlying's recent realised
volatility.

When IV sits below RV, the market is paying less than the underlying has
actually been moving — the delta may look conservative while the premium is
structurally too cheap. The agent refuses those entries regardless of how
attractive the strike appears.

Implemented in `executor.richness_ok()`, requiring IV/RV ≥ 1.05 against
10-day annualised close-to-close realised vol.

**Write-up line:** a volatility-richness gate prevents the agent from selling
premium into conditions where it is underpriced relative to realised movement.

---

## 3. Why orders ladder instead of crossing

On a two-leg spread there are two relevant prices. The **mid** is the midpoint
of both legs netted together. The **natural** is the short leg's bid minus the
long leg's ask — what an immediate market order actually receives. On a
short-dated index spread that gap is routinely 10–15 cents.

Against a $1.00 credit, crossing surrenders 10–15% of the position's entire
edge at the moment of entry. Since the modelled edge itself is a few percent of
width, execution slippage is not a rounding error — it is comparable in size to
the edge.

The agent therefore submits a net limit at mid, waits, cancels, and reprices
one cent lower, up to four steps, stopping at the credit floor. It will
abandon an unfilled position rather than chase. Implemented in
`executor.ladder_entry()` and `ladder_prices()`.

**Write-up line:** entries are worked as net limit orders from mid, never
crossed, with the credit floor as a hard stopping point.

---

## 4. Why multi-leg orders rather than two single-leg orders

Legging in sequentially creates a window in which one leg is filled and the
other is not, leaving a naked short put — an unbounded-risk position that the
strategy never intends to hold. Alpaca's multi-leg order class fills both legs
together or not at all, so the defined-risk structure is guaranteed at the
protocol level rather than assumed.

Implemented in `executor.build_mleg_payload()` using `order_class: "mleg"`.

**Write-up line:** all positions are submitted as atomic multi-leg orders, so
undefined risk is structurally unreachable rather than merely avoided.

---

## 5. Why the exit rules are asymmetric

Three distinct exit conditions, each with a different rationale:

- **Let it expire when the buyback costs under 5 cents.** Closing a nearly
  worthless spread means paying the bid-ask spread again. Below roughly 5
  cents the exit costs more than the residual risk it removes.
- **Close whenever the short leg goes in the money.** Assignment risk is
  operationally, not just financially, expensive. The agent runs unattended;
  it must never leave a position that could require intervention.
- **Close at 60% of credit captured.** The remaining 40% is earned slowly and
  carries increasing gamma risk into expiry. The asymmetry is deliberate.

Implemented in `executor.exit_decision()`.

---

## 6. Why the language model cannot enlarge a position

The model's role is adversarial review, not authorisation. It receives a
structured brief on a candidate that has already passed every deterministic
filter, and returns approve/veto plus a size multiplier.

That multiplier is clamped to [0, 1] in `risk.RiskGates.size()`. The model can
shrink or refuse; it cannot upsize, and it cannot reach a position that failed
a gate. Every gate is re-evaluated after the model's response, not before.

This is the central design claim: the model contributes judgement where
judgement is cheap to check, and holds no authority anywhere a mistake would
be expensive.

**Write-up line:** LLM output is clamped and re-gated; the model can veto or
reduce, never approve past a gate and never increase size.

---

## Outstanding item before trading

The sign convention for `limit_price` on a net-credit multi-leg order must be
confirmed with a single one-lot test order on day one, and `NET_CREDIT_SIGN`
in `executor.py` set accordingly. This is documented rather than guessed.
