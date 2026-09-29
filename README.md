# Flight Delay Insurance

A standalone Intelligent Contract on GenLayer: parametric flight-delay
insurance. Anyone can back a shared payout pool, anyone can buy coverage
for a specific flight, and GenLayer validators — not a claims adjuster —
decide whether a payout is owed by independently reading a live
flight-status page.

## Deployed contract

`0xe22165f2439904b2D5A290C109634416b1E090cE` (GenLayer Studionet)

## Live demo

<https://gleeful-otter-3929c3.netlify.app>

## What it does

- **`fund_pool()`** — payable. Anyone can back the shared pool that
  payouts are drawn from, like an underwriter.

- **`buy_policy(flight_number, flight_date, payout_amount)`** — payable.
  The attached GEN is the premium. `payout_amount` is what the
  policyholder receives if the flight turns out to be significantly
  delayed.
  - `flight_date` must be a future date (`YYYY-MM-DD`): coverage can only
    be bought before departure, never after the outcome is already known.
  - The evidence source is **derived by the contract** from `flight_number`
    **and** `flight_date` together
    (`https://www.flightaware.com/live/flight/{flight_number}/history/{flight_date}`)
    rather than supplied by the buyer, so a policyholder can never point
    validators at a page they control, and the same URL can never be
    reused across different dates for the same flight.
  - The payout is **reserved** out of the pool's available liquidity at
    purchase time. If the pool can't currently cover it, the purchase
    reverts instead of creating unfunded coverage.

- **`evaluate_flight(policy_id)`** — only callable after the flight's
  scheduled date has passed. Each validator independently fetches the
  derived `status_url`. Before any LLM judgment runs, a deterministic
  check (identical for every validator) verifies that the fetched page
  contains both the flight number and the date; if it doesn't, the result
  is `undetermined` and the LLM is never called. Only evidence that passes
  this check is handed to the LLM, which classifies the flight as
  `delayed` (2+ hours late), `on_time`, or `undetermined`. Validators only
  need to agree on the classification, not on the exact minute estimate or
  reasoning text, since those legitimately vary between independent reads
  of a live page and LLM runs.

- **`claim_payout(policy_id)`** — callable by the policyholder once a
  policy is `settled` or `disputed`:
  - `settled` + `delayed` → pays out the full `payout_amount` (guaranteed
    available, since it was reserved at purchase).
  - `settled` + `on_time` → nothing to claim; the premium stays in the
    pool, same as a real insurance premium.
  - `settled` + `undetermined`, or `disputed` → the premium is refunded in
    full, since the flight's status couldn't be reliably determined.

  Every claim releases that policy's reservation back to the pool's
  available liquidity, regardless of which branch paid out.

### Lifecycle vs. verdict

Every policy tracks two things separately, same pattern as any GenLayer
oracle that needs to be trusted with money:

- **`status`** — where the policy is: `pending → checked → settled | disputed`
- **`verdict`** — what the evidence says: `"" | "delayed" | "on_time" | "undetermined"`

A single evaluation only ever produces a provisional `checked` policy. It
takes **two consecutive evaluations agreeing on the same verdict** for a
policy to `settle`. If a later evaluation disagrees instead, the policy
moves to `disputed`. **Both `settled` and `disputed` are terminal**:
`evaluate_flight()` reverts for a policy in either state, so a verdict
that people have already been paid out (or refunded) against can never
move again, even after that refund has already gone out.

## Why this matters

Real-world insurance depends on someone deciding, after the fact, whether
a covered event actually happened — normally a claims adjuster, which is
slow and trust-dependent. This contract shows that decision can instead be
made by independent AI validators reading the same public evidence and
reaching consensus, with the payout executing automatically and
trustlessly the moment they agree.

## Tests

`tests/direct/test_lifecycle.py` contains 14 executable pytest tests that
run the contract in GenLayer's **Direct Mode** (`genlayer-test` / `gltest`;
in-memory, no GenLayer Studio or Docker required). Web and LLM responses
are mocked, and the harness clock is controlled, so every test is
deterministic. They cover:

- **Timing** — purchase only before departure, evaluation only after.
- **Evidence** — the evidence URL is derived from flight number and date;
  evidence that does not confirm both yields `undetermined` without the
  LLM being called.
- **Solvency** — payouts are reserved, unfunded coverage is rejected, and
  a settled policy releases its reservation on claim.
- **Payouts** — a `delayed` policy pays the full `payout_amount`; a
  `disputed` policy refunds the premium.
- **Lifecycle** — `settled` and `disputed` are terminal, even after a
  refund.

See [TESTS.md](TESTS.md) for the mapping from each guarantee to its tests.

### Running the tests

Requires Python 3.12+. Verified with `genlayer-test` 0.29.2 on Python 3.14.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install genlayer-test

# genlayer-test 0.29.2 downloads a GenVM archive named
# `genvm-universal.tar.xz`, which the current GenVM release (v0.3.0-rc7)
# does not publish, so direct-mode tests fail with an HTTP 404 before they
# start. Place the archive that the release does publish in the cache under
# the expected name, once:
mkdir -p ~/.cache/gltest-direct
curl -fLsS https://github.com/genlayerlabs/genvm/releases/download/v0.3.0-rc7/genvm-runners-all.tar.xz \
  -o ~/.cache/gltest-direct/genvm-universal-v0.3.0-rc7.tar.xz

gltest tests/ -v
```

Expected result: `14 passed`.

## Files

- `contracts/flight_delay_insurance.py` — the contract (Python, GenLayer SDK).
- `tests/direct/test_lifecycle.py` — the executable Direct Mode tests.
- `pyproject.toml` — points `gltest` at the `contracts/` directory.
- `TESTS.md` — maps each guarantee to the tests that verify it.
- `index.html` — a dependency-free frontend (`genlayer-js` only, no build
  step) for funding the pool, buying policies, triggering evaluations, and
  claiming payouts.

## Known limitations

- A policy's payout reservation is released when its policyholder calls
  `claim_payout()`. For a `settled` + `on_time` policy there is nothing to
  claim, so the reservation stays held until the policyholder calls it.
- Timing works at date granularity: a flight can only be insured for a
  date strictly after today, and evaluated only once that date has passed.
- The deterministic evidence check is a substring check for the flight
  number and date on the fetched page; `flight_number` is not validated
  against a stricter format.
