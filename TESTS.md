# Lifecycle Tests

The authoritative tests live in
[`tests/direct/test_lifecycle.py`](./tests/direct/test_lifecycle.py) —
real, executable pytest tests using GenLayer's official testing suite
(`genlayer-test` / `gltest`) in Direct Mode.

```
pip install genlayer-test
gltest tests/ -v
```

Every guarantee below runs as an actual test against the deployed
contract code — timing guards, evidence-URL derivation, evidence
authentication, solvency, and the disputed/terminal lifecycle. The
timing-gated guarantees (evaluation only after departure, dispute
happening after real elapsed time) use `direct_vm.warp(...)` to advance
the harness's clock, so `evaluate_flight()` can genuinely run past the
departure-timing guard within a single Direct Mode test run — no real
wall-clock waiting, and no skipped tests standing in for coverage that
isn't actually exercised.

## Test 1 — Timing: purchase only before departure, evaluation only after

1. `buy_policy("UA245", "2020-01-01", payout_amount)` (a date in the
   past) with GEN attached → **must revert**: "Coverage can only be
   purchased for a flight that hasn't departed yet."
2. `buy_policy("UA245", "<future date>", payout_amount)` with GEN
   attached → succeeds. Status `pending`.
3. `evaluate_flight(policy_id)` called immediately (flight date still in
   the future) → **must revert**: "This flight hasn't departed yet."
4. `direct_vm.warp(timedelta(days=3))` advances the harness clock past
   the flight's date, then `evaluate_flight(policy_id)` → succeeds,
   moving the policy to `checked`.

Expected result: steps 1 and 3 both revert; step 4 demonstrates the same
policy can be evaluated once real time (simulated via `warp`) has passed.

## Test 2 — Evidence: derived from flight + date, and authenticated before adjudication

1. Call `buy_policy("UA245", "<future date>", payout_amount)` — the
   function signature doesn't accept a `status_url` parameter at all.
2. `get_policy(policy_id).status_url` → returns
   `https://www.flightaware.com/live/flight/UA245/history/<that date>`,
   built entirely from `flight_number` **and** `flight_date` together.
   Buying the same flight for a different date produces a different URL.
3. After warping past the flight date, `evaluate_flight(policy_id)`
   fetches `status_url`. A deterministic check — plain code, not the LLM —
   verifies the fetched page actually contains both the flight number
   and the date.
4. When the mocked evidence page contains neither (a page that clearly
   does not confirm the flight), the result is `verdict: "undetermined"`
   — even when the mocked LLM is deliberately configured to say
   `"delayed"`, proving the deterministic check runs and can override the
   LLM's output, not just precede it in the code.

Expected result: the evidence source is fully derived and date-bound, and
a page failing to confirm both fields is rejected deterministically
before any LLM judgment — tested directly against the contract, not
just documented.

## Test 3 — Solvency: payouts are reserved, unfunded coverage is rejected

1. Deploy fresh, then `fund_pool()` with `100` GEN.
2. `buy_policy("UA100", "<future date>", payout_amount=80)` with a `10`
   GEN premium → succeeds. `pool_balance` is now `110`;
   `reserved_total` is `80`; `available_liquidity()` returns `30`.
3. `buy_policy("UA200", "<future date>", payout_amount=50)` with a `5`
   GEN premium → **must revert**: "Insufficient pool liquidity to cover
   this payout." (`available_liquidity()` was `30`, but `50` was
   requested.)
4. After warping past the flight date and settling the first policy as
   `on_time` (two matching `evaluate_flight()` calls), `claim_payout()`
   on it pays out `0` and releases its `80` GEN reservation —
   `available_liquidity()` returns to `110`.

Expected result: step 3 reverts because the pool can't cover every open
promise at once; step 4 shows a settled policy's reservation freeing up
capacity for future policies — both asserted directly against contract
state, not just described.

## Test 4 — Lifecycle: settled and disputed are both terminal

1. Buy a policy, warp past its flight date, get it to `checked` (one
   `evaluate_flight()` call).
2. `evaluate_flight()` again with a disagreeing verdict → status becomes
   `disputed`.
3. `claim_payout()` from the policyholder → refunds the premium in full.
4. `evaluate_flight()` again on the same policy, even with evidence that
   would clearly resolve it → **must revert**: "Policy already
   settled..." This holds even though a refund has already been paid
   out.
5. Separately, a different policy is warped past its date, settled with
   two matching evaluations (`settled`), and `evaluate_flight()` is
   called on it again → **must also revert**, for the same reason.

Expected result: once a policy leaves `checked` for either `settled` or
`disputed`, `evaluate_flight()` reverts unconditionally — verified by
actually calling it again after settlement/refund and asserting the
revert, not by a documented-but-unexecuted scenario.
