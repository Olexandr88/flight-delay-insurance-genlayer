# Tests

All guarantees below are verified by executable tests in
[`tests/direct/test_lifecycle.py`](tests/direct/test_lifecycle.py), which run
the contract in GenLayer's Direct Mode (`genlayer-test` / `gltest`). Web and
LLM responses are mocked and the harness clock is controlled, so the suite is
deterministic.

```bash
gltest tests/ -v      # expected: 14 passed
```

Setup instructions, including a required one-time workaround for
`genlayer-test` 0.29.2, are in the [README](README.md#running-the-tests).

## Guarantees and the tests that verify them

### Timing: purchase only before departure, evaluation only after

| Test | What it checks |
| --- | --- |
| `test_buy_policy_rejects_past_date` | Buying coverage for a past date reverts |
| `test_buy_policy_succeeds_for_future_date` | Buying for a future date creates a `pending` policy |
| `test_evaluate_reverts_before_departure` | Evaluating before the flight date reverts |
| `test_evaluate_succeeds_after_departure` | After the clock passes the flight date, evaluation succeeds and the policy becomes `checked` |

### Evidence: derived source, bound to flight number and date

| Test | What it checks |
| --- | --- |
| `test_status_url_is_derived_from_flight_and_date` | The stored `status_url` contains both the flight number and the date |
| `test_different_dates_produce_different_urls` | The same flight on two dates yields two different URLs |
| `test_mismatched_evidence_yields_undetermined_without_llm` | A page with the right flight number but a different date yields `undetermined` and never reaches the LLM |

### Solvency: payouts are reserved, unfunded coverage is rejected

| Test | What it checks |
| --- | --- |
| `test_unfunded_purchase_reverts` | A policy the pool cannot cover reverts |
| `test_funded_purchase_within_liquidity_succeeds` | A funded pool accepts the policy and available liquidity drops by the reserved payout |
| `test_second_purchase_beyond_liquidity_reverts` | A second policy that would exceed remaining liquidity reverts |
| `test_settled_on_time_policy_releases_reservation` | An `on_time` policy settles, and claiming releases its reservation while the premium stays in the pool |
| `test_delayed_settled_policy_pays_full_payout` | A `delayed` policy settles, pays the full `payout_amount`, and cannot be claimed twice |

### Lifecycle: `settled` and `disputed` are terminal

| Test | What it checks |
| --- | --- |
| `test_disputed_then_refund_then_evaluate_reverts` | Conflicting verdicts move the policy to `disputed`; the premium is refunded on claim; re-evaluation afterwards reverts |
| `test_settled_policy_rejects_further_evaluation_regression` | After two matching verdicts the policy is `settled` with two confirmations; further evaluation reverts |
