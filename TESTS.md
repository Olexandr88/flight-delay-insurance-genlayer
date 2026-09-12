# Lifecycle Tests

The authoritative tests live in
[`tests/direct/test_lifecycle.py`](./tests/direct/test_lifecycle.py) —
real, executable pytest tests using GenLayer's official testing suite
(`genlayer-test` / `gltest`) in Direct Mode.

```
pip install genlayer-test
gltest tests/ -v
```

Most guarantees (timing guards, evidence-URL derivation, solvency) run
directly in Direct Mode. Two guarantees — evidence authentication and the
disputed/terminal lifecycle — need `evaluate_flight()` to run *after* a
flight's date has genuinely elapsed, which Direct Mode's in-memory
execution can't fast-forward through. Those are documented as skipped
tests with exact integration-mode steps in their docstrings, rather than
faked. This document is a narrative walkthrough of everything the suite
covers, for readers who want the reasoning without running it.

## Test 1 — Timing: purchase only before departure, evaluation only after

1. `buy_policy("UA245", "2020-01-01", payout_amount)` (a date in the
   past) with GEN attached → **must revert**: "Coverage can only be
   purchased for a flight that hasn't departed yet."
2. `buy_policy("UA245", "<tomorrow's date>", payout_amount)` with GEN
   attached → succeeds. Status `pending`.
3. `evaluate_flight(policy_id)` called immediately (flight date still in
   the future) → **must revert**: "This flight hasn't departed yet."
4. Wait until the flight date has passed (or buy a policy for a date
   that has already elapsed as of testing, purchased before that date
   originally), then call `evaluate_flight(policy_id)` → succeeds.

Expected result: steps 1 and 3 both revert; buying is only possible
before departure, evaluating only after it.

## Test 2 — Evidence: derived from flight + date, and authenticated before adjudication

1. Call `buy_policy("UA245", "<future date>", payout_amount)` — notice
   the function signature no longer accepts a `status_url` parameter at
   all.
2. `get_policy(policy_id).status_url` → returns
   `https://www.flightaware.com/live/flight/UA245/history/<that date>`,
   built entirely from `flight_number` **and** `flight_date` together.
   Buying the same flight for a different date produces a different URL
   — the derivation ties them together deterministically, so a
   policyholder can never make one policy's evidence resolve to a
   different date's page. There is no way to make it resolve to an
   attacker-controlled page — the buyer only ever chooses which real
   flight to insure, never which webpage validators read.

Expected result: the evidence source is fully derived by the contract,
binding both the flight number and the exact date.

## Test 2b — Evidence authentication runs deterministically, before any LLM call

3. After the flight's date has elapsed, `evaluate_flight(policy_id)`
   fetches `status_url`. Every validator independently checks, in plain
   code (not via the LLM): does the fetched page contain the flight
   number? Does it contain the flight date? If either check fails, the
   result is `verdict: "undetermined"` — assembled directly by the
   contract, without ever calling the LLM.
4. Only once both checks pass does the fetched page get handed to the
   LLM for the actual delay judgment.

Expected result: a page that fails to confirm both the flight number and
date is rejected deterministically and identically by every validator,
before any nondeterministic LLM judgment is even attempted — so an LLM
can never be asked to adjudicate against evidence that doesn't
demonstrably match the policy.

## Test 3 — Solvency: payouts are reserved, unfunded coverage is rejected

1. Deploy fresh, then `fund_pool()` with `100` GEN.
2. `buy_policy("UA100", "<future date>", payout_amount=80)` with a `10`
   GEN premium → succeeds. `pool_balance` is now `110`;
   `reserved_total` is `80`; `available_liquidity()` returns `30`.
3. `buy_policy("UA200", "<future date>", payout_amount=50)` with a `5`
   GEN premium → **must revert**: "Insufficient pool liquidity to cover
   this payout." (`available_liquidity()` was `30`, but `50` was
   requested.)
4. `buy_policy("UA200", "<future date>", payout_amount=25)` with a `5`
   GEN premium → succeeds, since `25 <= 30`. `reserved_total` is now
   `105`; `available_liquidity()` returns `10`.
5. Settle policy `UA100` as `on_time` (two matching `evaluate_flight()`
   calls after its date passes), then `claim_payout()` on it → pays out
   `0`, and releases its `80` GEN reservation. `available_liquidity()`
   is now `90`.

Expected result: step 3 reverts because the pool can't cover every open
promise at once; step 5 shows a settled policy's reservation freeing up
capacity for future policies.

## Test 4 — Lifecycle: settled and disputed are both terminal

1. Get a policy to `checked` (one `evaluate_flight()` call after its
   date has passed).
2. `evaluate_flight()` again with a disagreeing verdict → status becomes
   `disputed`.
3. `claim_payout()` from the policyholder → refunds the premium in full.
4. `evaluate_flight()` again on the same policy, even with evidence that
   would clearly resolve it → **must revert**: "Policy already settled —
   settled and disputed policies are both terminal..." This holds
   regardless of the fact that a refund has already been paid out.
5. Separately, get a different policy to `settled` (two consecutive
   matching evaluations), `claim_payout()` it, then call
   `evaluate_flight()` on it again → **must also revert**, for the same
   reason.

Expected result: once a policy leaves the `checked` state for either
`settled` or `disputed`, `evaluate_flight()` reverts unconditionally —
there is no path back to an active state, whether or not money has
already moved.
