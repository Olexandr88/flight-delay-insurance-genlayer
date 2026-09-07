# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
from dataclasses import dataclass
import datetime
import typing

DELAY_THRESHOLD_MINUTES = 120

# Used to send GEN back to a policyholder's EOA. Sending to an address on
# the GenLayer Chain layer (EOA or EVM contract) is an *external* message
# and goes through this IC's ghost contract — see "Value Transfers" in the
# GenLayer docs. The empty View/Write bodies are intentional: we only ever
# call emit_transfer() on this interface, never a named method.
@gl.evm.contract_interface
class _EOA:
    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Policy:
    policyholder: Address
    flight_number: str
    flight_date: str
    status_url: str
    premium: u256
    payout_amount: u256
    status: str  # "pending" -> "checked" -> "settled" | "disputed"
    verdict: str  # "" | "delayed" | "on_time" | "undetermined"
    delay_minutes: u32
    evaluations: u32
    confirmations: u32
    claimed: bool


class FlightDelayInsurance(gl.Contract):
    """
    A standalone Intelligent Contract: parametric flight-delay insurance.

    Anyone can back the shared payout pool. Anyone can buy a policy for a
    specific flight, before it departs, by paying a premium and naming the
    payout they want if the flight is significantly delayed. After the
    flight's scheduled date, anyone can trigger evaluate_flight():
    GenLayer validators independently read a live flight-status page and
    reach consensus on whether the flight was delayed, on time, or
    undetermined. A policy only settles after two consecutive matching
    evaluations. Once settled, the policyholder calls claim_payout() to
    receive their payout (if delayed), a full premium refund (if
    undetermined or disputed), or nothing (if on time — the premium stays
    in the pool, same as any real insurance premium).

    Every payout is reserved out of the pool at purchase time, so a
    policy's promised payout can never be double-committed against
    liquidity another policy is also counting on.
    """

    policies: TreeMap[u32, Policy]
    next_id: u32
    pool_balance: u256
    reserved_total: u256  # sum of payout_amount for every unsettled policy

    def __init__(self):
        self.next_id = u32(0)
        self.pool_balance = u256(0)
        self.reserved_total = u256(0)

    # ------------------------------------------------------------------
    # Funding the shared payout pool
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def fund_pool(self) -> None:
        """Anyone can back the pool that policy payouts are drawn from."""
        self.pool_balance = u256(self.pool_balance + gl.message.value)

    @gl.public.view
    def available_liquidity(self) -> u256:
        """Pool balance minus everything already promised to open policies."""
        if self.reserved_total >= self.pool_balance:
            return u256(0)
        return u256(self.pool_balance - self.reserved_total)

    # ------------------------------------------------------------------
    # Buying a policy
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def buy_policy(
        self,
        flight_number: str,
        flight_date: str,
        payout_amount: u256,
    ) -> u32:
        """
        Pays the attached GEN as the premium for a new policy on the given
        flight. payout_amount is what the policyholder will receive if the
        flight turns out to be significantly delayed.

        flight_date must be a future date ("YYYY-MM-DD") — coverage can
        only be bought before departure, never after the outcome is
        already known.

        The evidence source is derived from flight_number, not supplied by
        the buyer: this is a public, well-known flight-tracking URL
        pattern, not a page the policyholder could control or fake.
        """
        premium = gl.message.value
        if premium == u256(0):
            raise gl.vm.UserError("Send some GEN as the premium")
        if payout_amount == u256(0):
            raise gl.vm.UserError("payout_amount must be greater than zero")

        try:
            parsed_date = datetime.date.fromisoformat(flight_date)
        except ValueError:
            raise gl.vm.UserError("flight_date must be in YYYY-MM-DD format")

        if parsed_date <= datetime.datetime.now().date():
            raise gl.vm.UserError(
                "Coverage can only be purchased for a flight that hasn't "
                "departed yet — flight_date must be a future date"
            )

        # Reserve this payout against the pool's *available* liquidity —
        # i.e. what's left after every other open policy's own promised
        # payout has already been set aside. A policy is never allowed to
        # exist unless the pool can actually cover it.
        self.pool_balance = u256(self.pool_balance + premium)
        available = u256(0)
        if self.pool_balance > self.reserved_total:
            available = u256(self.pool_balance - self.reserved_total)
        if available < payout_amount:
            raise gl.vm.UserError(
                "Insufficient pool liquidity to cover this payout — "
                "unfunded coverage is rejected. Ask someone to fund_pool() "
                "first, or request a smaller payout_amount."
            )
        self.reserved_total = u256(self.reserved_total + payout_amount)

        status_url = f"https://www.flightaware.com/live/flight/{flight_number}"

        policy_id = self.next_id
        self.policies[policy_id] = Policy(
            policyholder=gl.message.sender_address,
            flight_number=flight_number,
            flight_date=flight_date,
            status_url=status_url,
            premium=premium,
            payout_amount=payout_amount,
            status="pending",
            verdict="",
            delay_minutes=u32(0),
            evaluations=u32(0),
            confirmations=u32(0),
            claimed=False,
        )
        self.next_id = u32(self.next_id + 1)
        return policy_id

    # ------------------------------------------------------------------
    # Evaluating whether the flight was delayed
    # ------------------------------------------------------------------

    @gl.public.write
    def evaluate_flight(self, policy_id: u32) -> typing.Any:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy id")
        policy = self.policies[policy_id]

        # A settled or disputed policy is terminal: its payout has either
        # already been claimed or is permanently a full refund. It must
        # never be re-evaluated — otherwise a verdict could flip after
        # money has already moved.
        if policy.status in ("settled", "disputed"):
            raise gl.vm.UserError(
                "Policy already settled — settled and disputed policies "
                "are both terminal and can never be re-evaluated"
            )

        scheduled = datetime.date.fromisoformat(policy.flight_date)
        if scheduled >= datetime.datetime.now().date():
            raise gl.vm.UserError(
                "This flight hasn't departed yet — evaluation is only "
                "possible after the scheduled flight date has passed"
            )

        status_url = policy.status_url
        flight_number = policy.flight_number
        flight_date = policy.flight_date

        prompt = f"""You are assessing a parametric flight-delay insurance claim.

FLIGHT: {flight_number} on {flight_date}
DELAY THRESHOLD FOR PAYOUT: {DELAY_THRESHOLD_MINUTES} minutes

Read the flight-status evidence below and determine the flight's actual
arrival delay in minutes relative to its scheduled arrival time.

Respond using ONLY the following JSON format, nothing else:
{{
  "verdict": str,        // "delayed" if delay_minutes >= {DELAY_THRESHOLD_MINUTES}, "on_time" if it flew with a smaller delay or none, "undetermined" if the evidence does not clearly show the flight's status
  "delay_minutes": int,  // best estimate of the delay in minutes (0 if on time or undetermined)
  "reasoning": str       // one or two sentences citing what in the evidence supports this
}}
This result must be perfectly parsable by a JSON parser without errors.
"""

        def leader_fn():
            response = gl.nondet.web.get(status_url)
            web_data = response.body.decode("utf-8")
            full_prompt = prompt + f"\n\nEVIDENCE:\n{web_data}"
            return gl.nondet.exec_prompt(full_prompt, response_format="json")

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            leader_out = leaders_res.calldata
            if "verdict" not in leader_out or "reasoning" not in leader_out:
                return False
            if not leader_out["reasoning"]:
                return False
            if leader_out["verdict"] not in ("delayed", "on_time", "undetermined"):
                return False
            my_result = leader_fn()
            if my_result.get("verdict") not in ("delayed", "on_time", "undetermined"):
                return False
            # Only the verdict classification must match across validators —
            # the exact minute estimate and reasoning text are allowed to
            # vary between independent reads of a live page and LLM runs.
            return my_result["verdict"] == leader_out["verdict"]

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        new_verdict = result["verdict"]
        previous_verdict = policy.verdict

        policy.evaluations = u32(policy.evaluations + 1)

        if policy.status == "pending":
            policy.status = "checked"
            policy.confirmations = u32(1)
        elif new_verdict == previous_verdict:
            policy.confirmations = u32(policy.confirmations + 1)
            policy.status = "settled" if policy.confirmations >= u32(2) else "checked"
        else:
            policy.status = "disputed"
            policy.confirmations = u32(1)

        policy.verdict = new_verdict
        policy.delay_minutes = u32(result.get("delay_minutes", 0))
        self.policies[policy_id] = policy

        return {
            "policy_id": policy_id,
            "status": policy.status,
            "verdict": policy.verdict,
            "delay_minutes": policy.delay_minutes,
            "confirmations": policy.confirmations,
        }

    # ------------------------------------------------------------------
    # Claiming a payout / refund
    # ------------------------------------------------------------------

    @gl.public.write
    def claim_payout(self, policy_id: u32) -> u256:
        """
        - status == "settled" and verdict == "delayed": pays out the full
          payout_amount — this is guaranteed available, since it was
          reserved out of the pool back when the policy was purchased.
        - status == "settled" and verdict == "on_time": nothing to claim;
          the premium stays in the pool, same as a real insurance premium.
        - status == "settled" and verdict == "undetermined", or
          status == "disputed": the flight's status could not be reliably
          determined, so the premium is refunded in full.

        In every case, this policy's reservation is released back to the
        pool's available liquidity — a settled policy never keeps holding
        capacity hostage from other policies.
        """
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy id")
        policy = self.policies[policy_id]

        if gl.message.sender_address != policy.policyholder:
            raise gl.vm.UserError("Only the policyholder can claim this policy")
        if policy.claimed:
            raise gl.vm.UserError("Already claimed")
        if policy.status not in ("settled", "disputed"):
            raise gl.vm.UserError("Policy is not settled or disputed yet")

        if policy.status == "settled" and policy.verdict == "delayed":
            payout = policy.payout_amount
        elif policy.status == "settled" and policy.verdict == "on_time":
            payout = u256(0)
        else:  # "undetermined" (settled) or "disputed"
            payout = policy.premium

        policy.claimed = True
        self.policies[policy_id] = policy

        # Release this policy's reservation now that it's settled, whether
        # or not the full reserved amount was actually paid out.
        if self.reserved_total >= policy.payout_amount:
            self.reserved_total = u256(self.reserved_total - policy.payout_amount)
        else:
            self.reserved_total = u256(0)

        if payout > u256(0):
            if payout > self.pool_balance:
                payout = self.pool_balance
            self.pool_balance = u256(self.pool_balance - payout)
            _EOA(policy.policyholder).emit_transfer(value=payout)

        return payout

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @gl.public.view
    def get_policy(self, policy_id: u32) -> typing.Any:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy id")
        p = self.policies[policy_id]
        return {
            "policyholder": p.policyholder,
            "flight_number": p.flight_number,
            "flight_date": p.flight_date,
            "status_url": p.status_url,
            "premium": p.premium,
            "payout_amount": p.payout_amount,
            "status": p.status,
            "verdict": p.verdict,
            "delay_minutes": p.delay_minutes,
            "evaluations": p.evaluations,
            "confirmations": p.confirmations,
            "claimed": p.claimed,
        }

    @gl.public.view
    def get_pool_balance(self) -> u256:
        return self.pool_balance

    @gl.public.view
    def get_reserved_total(self) -> u256:
        return self.reserved_total

    @gl.public.view
    def total_policies(self) -> u32:
        return self.next_id
