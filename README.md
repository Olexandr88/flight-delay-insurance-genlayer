# Flight Delay Insurance

A standalone Intelligent Contract on GenLayer: parametric flight-delay
insurance. Anyone can back a shared payout pool, anyone can buy coverage
for a specific flight, and GenLayer validators — not a claims adjuster —
decide whether a payout is owed by independently reading a live
flight-status page.

## Deployed contract

`0x5b3433B857619C2e126CA95c0cD28dC63E59Ac87` (GenLayer Studionet)

## Live demo

https://courageous-otter-9bfc0e.netlify.app

## What it does

- **`fund_pool()`** — payable. Anyone can back the shared pool that
  payouts are drawn from, like an underwriter.
- **`buy_policy(flight_number, flight_date, payout_amount)`** — payable.
  The attached GEN is the premium. `payout_amount` is what the
  policyholder receives if the flight turns out to be significantly
  delayed. `flight_date` must be a future date (`YYYY-MM-DD`) — coverage
  can only be bought before departure, never after the outcome is
  already known. The evidence source is **derived automatically** from
  `flight_number` (`https://www.flightaware.com/live/flight/{flight_number}`)
  rather than supplied by the buyer, so a policyholder can never point
  validators at a page they control. The payout is also **reserved**
  out of the pool's available liquidity at purchase time — if the pool
  can't currently cover it, the purchase reverts rather than create
  unfunded coverage.
- **`evaluate_flight(policy_id)`** — only callable after the flight's
  scheduled date has passed. Each validator independently fetches the
  derived `status_url` and asks an LLM to classify the flight as
  `delayed` (2+ hours late), `on_time`, or `undetermined`, using a
  custom Equivalence Principle check that only requires validators to
  agree on the classification — not on the exact minute estimate or
  reasoning text, since those legitimately vary between independent
  reads of a live page and LLM runs.
- **`claim_payout(policy_id)`** — once a policy is `settled` or
  `disputed`:
  - `settled` + `delayed` → pays out the full `payout_amount`
    (guaranteed available, since it was reserved at purchase).
  - `settled` + `on_time` → nothing to claim; the premium stays in the
    pool, same as a real insurance premium.
  - `settled` + `undetermined`, or `disputed` → the premium is refunded
    in full, since the flight's status couldn't be reliably determined.

  Every claim releases that policy's reservation back to the pool's
  available liquidity, regardless of which branch paid out.

### Lifecycle vs. verdict

Every policy tracks two things separately, same pattern as any
GenLayer oracle that needs to be trusted with money:

- **`status`** — where the policy is: `pending → checked → settled | disputed`
- **`verdict`** — what the evidence says: `"" | "delayed" | "on_time" | "undetermined"`

A single evaluation only ever produces a provisional `checked` policy. It
takes **two consecutive evaluations agreeing on the same verdict** for a
policy to `settle`. If a later evaluation disagrees instead, the policy
moves to `disputed`. **Both `settled` and `disputed` are terminal** —
`evaluate_flight()` reverts for a policy in either state, so a verdict
people have already been paid out (or refunded) against can never move
again, even after that refund has already gone out. See
[TESTS.md](./TESTS.md) for the exact call sequences that verify this,
along with the timing, evidence, and solvency guarantees above.

## Why this matters

Real-world insurance depends on someone deciding, after the fact,
whether a covered event actually happened — normally a claims adjuster,
which is slow and trust-dependent. This contract shows that decision can
instead be made by independent AI validators reading the same public
evidence and reaching consensus, with the payout executing automatically
and trustlessly the moment they agree.

## Files

- `flight_delay_insurance.py` — the contract (Python, GenLayer SDK).
- `index.html` — a dependency-free frontend (`genlayer-js` only, no
  build step) for funding the pool, buying policies, triggering
  evaluations, and claiming payouts.
