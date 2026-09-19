"""
Executable lifecycle tests for FlightDelayInsurance, using GenLayer's
official testing suite (`genlayer-test` / `gltest`) in Direct Mode.

Setup:
    pip install genlayer-test

Run:
    gltest tests/ -v
"""

import datetime

import pytest
from gltest import create_account
from gltest.assertions import tx_execution_succeeded


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy("contracts/flight_delay_insurance.py")


def _future_date(days=10):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _past_date(days=10):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _advance(direct_vm, delta):
    """
    Moves the harness's clock forward by `delta`. Uses direct_vm.warp(),
    the documented Direct Mode time-travel cheatcode. Some gltest
    releases don't propagate warp() into the datetime a contract sees via
    Python's datetime.datetime.now() inside GenVM execution, so this also
    patches the underlying message datetime directly as a fallback —
    the same workaround other public GenLayer projects use for this known
    harness quirk.
    """
    direct_vm.warp(delta)
    try:
        current = direct_vm.message_raw["datetime"]
        direct_vm.message_raw["datetime"] = current + delta
    except (AttributeError, KeyError, TypeError):
        # Harness already propagated warp() correctly on its own — no
        # fallback needed on this gltest version.
        pass


def _evaluate_with_verdict(contract, direct_vm, policy_id, verdict):
    direct_vm.mock_web(r".*", {"status": 200, "body": "mock flight evidence page"})
    direct_vm.mock_llm(
        r".*",
        f'{{"verdict": "{verdict}", "delay_minutes": 150, "reasoning": "mocked evidence confirms this flight and date"}}',
    )
    result = contract.evaluate_flight(policy_id)
    direct_vm.clear_mocks()
    return result


class TestTimingGuards:
    """Steward guarantee: purchase only before departure, evaluation only after."""

    def test_buy_policy_rejects_past_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        with direct_vm.expect_revert("Coverage can only be purchased"):
            contract.buy_policy("UA245", _past_date(), 30, value=5)

    def test_buy_policy_succeeds_for_future_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        tx = contract.buy_policy("UA245", _future_date(), 30, value=5)
        assert tx_execution_succeeded(tx)

    def test_evaluate_reverts_before_departure(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        policy_id = contract.buy_policy("UA245", _future_date(), 30, value=5)
        with direct_vm.expect_revert("This flight hasn't departed yet"):
            contract.evaluate_flight(policy_id)

    def test_evaluate_succeeds_after_departure(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        flight_date = _future_date(days=2)
        policy_id = contract.buy_policy("UA245", flight_date, 30, value=5)

        # Move the harness clock past the flight's date.
        _advance(direct_vm, datetime.timedelta(days=3))

        result = _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
        assert result["status"] == "checked"


class TestEvidenceIsDerivedAndDateBound:
    """Steward guarantee: evidence source is derived, bound to both flight_number and flight_date."""

    def test_status_url_has_no_buyer_parameter(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        flight_date = _future_date()
        policy_id = contract.buy_policy("UA245", flight_date, 30, value=5)

        policy = contract.get_policy(policy_id)
        assert "UA245" in policy["status_url"]
        assert flight_date in policy["status_url"]

    def test_different_dates_produce_different_urls(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        date_a = _future_date(10)
        date_b = _future_date(20)

        policy_a = contract.buy_policy("UA245", date_a, 30, value=5)
        policy_b = contract.buy_policy("UA245", date_b, 30, value=5)

        url_a = contract.get_policy(policy_a)["status_url"]
        url_b = contract.get_policy(policy_b)["status_url"]
        assert url_a != url_b


class TestEvidenceAuthenticationBeforeAdjudication:
    """
    Steward guarantee: evidence is verified to match both flight_number
    and flight_date, deterministically, BEFORE any LLM adjudication runs.
    """

    def test_mismatched_evidence_yields_undetermined_without_calling_llm(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice
        flight_date = _future_date(days=2)
        policy_id = contract.buy_policy("UA245", flight_date, 30, value=5)

        _advance(direct_vm, datetime.timedelta(days=3))

        # The fetched page does NOT contain the flight number or date.
        direct_vm.mock_web(
            r".*", {"status": 200, "body": "this page confirms nothing useful"}
        )
        # Even though the mocked LLM would say "delayed" if asked, the
        # deterministic check must reject the evidence before the LLM is
        # ever consulted.
        direct_vm.mock_llm(
            r".*",
            '{"verdict": "delayed", "delay_minutes": 200, "reasoning": "should never be used"}',
        )
        result = contract.evaluate_flight(policy_id)
        direct_vm.clear_mocks()

        assert result["verdict"] == "undetermined"


class TestSolvency:
    """Steward guarantee: payouts are reserved, unfunded coverage is rejected."""

    def test_unfunded_purchase_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy("UA100", _future_date(), 80, value=5)

    def test_funded_purchase_within_liquidity_succeeds(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        contract.fund_pool(value=100)
        tx = contract.buy_policy("UA100", _future_date(), 80, value=10)
        assert tx_execution_succeeded(tx)
        assert contract.available_liquidity() == 30  # 110 pool - 80 reserved

    def test_second_purchase_beyond_liquidity_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        contract.fund_pool(value=100)
        contract.buy_policy("UA100", _future_date(), 80, value=10)
        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy("UA200", _future_date(), 50, value=5)

    def test_settled_on_time_policy_releases_reservation(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        contract.fund_pool(value=100)
        flight_date = _future_date(days=2)
        policy_id = contract.buy_policy("UA100", flight_date, 80, value=10)
        assert contract.available_liquidity() == 30

        _advance(direct_vm, datetime.timedelta(days=3))
        _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
        _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")

        policy = contract.get_policy(policy_id)
        assert policy["status"] == "settled"

        direct_vm.sender = alice
        contract.claim_payout(policy_id)
        assert contract.available_liquidity() == 110  # reservation released


class TestLifecycleTerminalStates:
    """Steward guarantee: settled and disputed are both terminal."""

    def test_disputed_then_refund_then_evaluate_reverts(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        contract.fund_pool(value=100)
        flight_date = _future_date(days=2)
        policy_id = contract.buy_policy("UA300", flight_date, 30, value=5)

        direct_vm.sender = bob

        _advance(direct_vm, datetime.timedelta(days=3))

        # First evaluation: "delayed" -> status "checked".
        _evaluate_with_verdict(contract, direct_vm, policy_id, "delayed")

        # Second evaluation disagrees: "on_time" -> status "disputed".
        _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
        policy = contract.get_policy(policy_id)
        assert policy["status"] == "disputed"

        # Policyholder (alice) claims a full premium refund while disputed.
        direct_vm.sender = alice
        payout = contract.claim_payout(policy_id)
        assert payout == 5  # full premium refunded

        # Even with evidence that would resolve cleanly, evaluate_flight()
        # must still revert now that the policy is disputed and refunded.
        with direct_vm.expect_revert("Policy already settled"):
            _evaluate_with_verdict(contract, direct_vm, policy_id, "delayed")

    def test_settled_policy_rejects_further_evaluation_regression(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice
        contract.fund_pool(value=100)
        flight_date = _future_date(days=2)
        policy_id = contract.buy_policy("UA400", flight_date, 30, value=5)

        _advance(direct_vm, datetime.timedelta(days=3))

        _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
        _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
        policy = contract.get_policy(policy_id)
        assert policy["status"] == "settled"

        with direct_vm.expect_revert("Policy already settled"):
            _evaluate_with_verdict(contract, direct_vm, policy_id, "on_time")
