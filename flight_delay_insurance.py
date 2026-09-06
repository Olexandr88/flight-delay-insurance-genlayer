# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *
from dataclasses import dataclass
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
    specific flight by paying a premium and naming the payout they want if
    the flight is significantly delayed. After the scheduled date, anyone
    can trigger evaluate_flight(): GenLayer validators independently read
    a live flight-status page and reach consensus on whether the flight
    was delayed, on time, or undetermined. A policy only settles after two
    consecutive matching evaluations. Once settled, the policyholder calls
    claim_payout() to receive their payout (if delayed), a full premium
    refund (if undetermined or disputed), or nothing (if on time — the
    premium stays in the pool, same as any real insurance premium).
    """

    policies: TreeMap[u32, Policy]
    next_id: u32
    pool_balance: u256

    def __init__(self):
        self.next_id = u32(0)
        self.pool_balance = u256(0)

    # ------------------------------------------------------------------
    # Funding the shared payout pool
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def fund_pool(self) -> None:
        """Anyone can back the pool that policy payouts are drawn from."""
        self.pool_balance = u256(self.pool_balance + gl.message.value)

    # ------------------------------------------------------------------
    # Buying a policy
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def buy_policy(
        self,
        flight_number: str,
        flight_date: str,
        status_url: str,
        payout_amount: u256,
    ) -> u32:
        """
        Pays the attached GEN as the premium for a new policy on the given
        flight. payout_amount is what the policyholder will receive if the
        flight turns out to be significantly delayed.
        """
        premium = gl.message.value
        if premium == u256(0):
            raise gl.vm.UserError("Send some GEN as the premium")
        if payout_amount == u256(0):
            raise gl.vm.UserError("payout_amount must be greater than zero")

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
        self.pool_balance = u256(self.pool_balance + premium)
        return policy_id

    # ------------------------------------------------------------------
    # Evaluating whether the flight was delayed
    # ------------------------------------------------------------------

    @gl.public.write
    def evaluate_flight(self, policy_id: u32) -> typing.Any:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy id")
        policy = self.policies[policy_id]

        # Once settled, a policy's payout is fixed. It must never be
        # re-evaluated — otherwise a verdict could flip after a claim has
        # already been paid out.
        if policy.status == "settled":
            raise gl.vm.UserError("Policy already settled")

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
        - status == "settled" and verdict == "delayed": pays out
          min(payout_amount, pool_balance) — the pool never pays out more
          than it holds.
        - status == "settled" and verdict == "on_time": nothing to claim;
          the premium stays in the pool, same as a real insurance premium.
        - status == "settled" and verdict == "undetermined", or
          status == "disputed": the flight's status could not be reliably
          determined, so the premium is refunded in full.
        - Otherwise (still pending/checked): reverts.
        """
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy id")
        policy = self.policies[policy_id]

        if gl.message.sender_address != policy.policyholder:
            raise gl.vm.UserError("Only the policyholder can claim this policy")
        if policy.claimed:
            raise gl.vm.UserError("Already claimed")

        if policy.status == "settled":
            if policy.verdict == "delayed":
                payout = policy.payout_amount
                if payout > self.pool_balance:
                    payout = self.pool_balance
            elif policy.verdict == "on_time":
                payout = u256(0)
            else:  # "undetermined"
                payout = policy.premium
                if payout > self.pool_balance:
                    payout = self.pool_balance
        elif policy.status == "disputed":
            payout = policy.premium
            if payout > self.pool_balance:
                payout = self.pool_balance
        else:
            raise gl.vm.UserError("Policy is not settled or disputed yet")

        policy.claimed = True
        self.policies[policy_id] = policy

        if payout > u256(0):
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
    def total_policies(self) -> u32:
        return self.next_id
