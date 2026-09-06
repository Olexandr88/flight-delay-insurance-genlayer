# Flight Delay Insurance

A standalone Intelligent Contract on GenLayer: parametric flight-delay
insurance. Anyone can back a shared payout pool, anyone can buy coverage
for a specific flight, and GenLayer validators — not a claims adjuster —
decide whether a payout is owed by independently reading a live
flight-status page.

## Deployed contract

`0x4332e6ab7210Cc92167c6ab14e391d9C5ACe1D34` (GenLayer Studionet)

## Live demo

_add your Netlify URL here after deploying_

## What it does

- **`fund_pool()`** — payable. Anyone can back the shared pool that
  payouts are drawn from, like an underwriter.
- **`buy_policy(flight_number, flight_date, status_url, payout_amount)`**
  — payable. The attached GEN is the premium. `payout_amount` is what the
  policyholder receives if the flight turns out to be significantly
  delayed. `status_url` is the live page validators will check at
  settlement (e.g. a flight tracker page for that flight).
- **`evaluate_flight(policy_id)`** — after the flight's scheduled date,
  anyone can trigger this. Each validator independently fetches
  `status_url` and asks an LLM to classify the flight as `delayed`
  (2+ hours late), `on_time`, or `undetermined`, using a custom
  Equivalence Principle check that only requires validators to agree on
  the classification — not on the exact minute estimate or reasoning
  text, since those legitimately vary between independent reads of a live
  page and LLM runs.
- **`claim_payout(policy_id)`** — once a policy is `settled`:
  - `delayed` → pays out `min(payout_amount, pool_balance)`.
  - `on_time` → nothing to claim; the premium stays in the pool, same as
    a real insurance premium.
  - `undetermined`, or a policy that ended up `disputed` instead of
    settled → the premium is refunded in full, since the flight's status
    couldn't be reliably determined.

### Lifecycle vs. verdict

Every policy tracks two things separately, same pattern as any
GenLayer oracle that needs to be trusted with money:

- **`status`** — where the policy is: `pending → checked → settled | disputed`
- **`verdict`** — what the evidence says: `"" | "delayed" | "on_time" | "undetermined"`

A single evaluation only ever produces a provisional `checked` policy. It
takes **two consecutive evaluations agreeing on the same verdict** for a
policy to `settle`. If a later evaluation disagrees instead, the policy
moves to `disputed` (premium refunded). Once `settled`, a policy is
frozen — `evaluate_flight()` reverts rather than let a paid-out verdict
move again.

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
