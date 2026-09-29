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
from gltest import create_account, get_contract_factory
from gltest.assertions import tx_execution_succeeded


@pytest.fixture
def contract():
    factory = get_contract_factory("FlightDelayInsurance")
    return factory.deploy()


def _future_date(days=10):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _past_date(days=10):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _advance(direct_vm, delta):
    """Advance the Direct Mode harness clock."""
    direct_vm.warp(delta)


def _evaluate_with_verdict(contract, direct_vm, policy_id, verdict):
    """
    Evaluate a policy with deterministic web and LLM mocks.

    The mocked evidence intentionally contains the exact flight number and
    flight date stored in the policy so the contract's deterministic
    evidence-authentication step passes before the LLM is invoked.
    """
    policy = contract.get_policy(args=[policy_id]).call()

    flight_number = policy["flight_number"]
    flight_date = policy["flight_date"]

    direct_vm.mock_web(
        r".*",
        {
            "status": 200,
            "body": (
                f"Flight {flight_number} was operated on {flight_date}. "
                f"Flight status confirms the scheduled flight for that date."
            ),
        },
    )
    direct_vm.mock_llm(
        r".*",
        (
            f'{{"verdict": "{verdict}", '
            f'"delay_minutes": 150, '
            f'"reasoning": "Mocked evidence confirms flight '
            f'{flight_number} on {flight_date}."}}'
        ),
    )

    try:
        return contract.evaluate_flight(args=[policy_id]).transact()
    finally:
        direct_vm.clear_mocks()


class TestTimingGuards:
    """Steward guarantee: purchase only before departure, evaluation only after."""

    def test_buy_policy_rejects_past_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice

        with direct_vm.expect_revert("Coverage can only be purchased"):
            contract.buy_policy(
                args=["UA245", _past_date(), 30]
            ).transact(value=5)

    def test_buy_policy_succeeds_for_future_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice

        tx = contract.buy_policy(
            args=["UA245", _future_date(), 30]
        ).transact(value=5)

        assert tx_execution_succeeded(tx)

    def test_evaluate_reverts_before_departure(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice

        tx = contract.buy_policy(
            args=["UA245", _future_date(), 30]
        ).transact(value=5)
        policy_id = tx.return_value

        with direct_vm.expect_revert("This flight hasn't departed yet"):
            contract.evaluate_flight(args=[policy_id]).transact()

    def test_evaluate_succeeds_after_departure(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice

        flight_date = _future_date(days=2)

        tx = contract.buy_policy(
            args=["UA245", flight_date, 30]
        ).transact(value=5)
        policy_id = tx.return_value

        _advance(direct_vm, datetime.timedelta(days=3))

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        policy = contract.get_policy(args=[policy_id]).call()
        assert policy["status"] == "checked"


class TestEvidenceIsDerivedAndDateBound:
    """
    Steward guarantee: evidence source is derived and bound to both
    flight_number and flight_date.
    """

    def test_status_url_is_derived_from_flight_and_date(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        flight_date = _future_date()

        tx = contract.buy_policy(
            args=["UA245", flight_date, 30]
        ).transact(value=5)
        policy_id = tx.return_value

        policy = contract.get_policy(args=[policy_id]).call()

        assert "UA245" in policy["status_url"]
        assert flight_date in policy["status_url"]

    def test_different_dates_produce_different_urls(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        date_a = _future_date(10)
        date_b = _future_date(20)

        tx_a = contract.buy_policy(
            args=["UA245", date_a, 30]
        ).transact(value=5)

        tx_b = contract.buy_policy(
            args=["UA245", date_b, 30]
        ).transact(value=5)

        url_a = contract.get_policy(
            args=[tx_a.return_value]
        ).call()["status_url"]

        url_b = contract.get_policy(
            args=[tx_b.return_value]
        ).call()["status_url"]

        assert url_a != url_b


class TestEvidenceAuthenticationBeforeAdjudication:
    """
    Steward guarantee: evidence is verified against both flight_number
    and flight_date before LLM adjudication.
    """

    def test_mismatched_evidence_yields_undetermined_without_llm(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        flight_date = _future_date(days=2)

        tx = contract.buy_policy(
            args=["UA245", flight_date, 30]
        ).transact(value=5)
        policy_id = tx.return_value

        _advance(direct_vm, datetime.timedelta(days=3))

        # Correct flight number, deliberately wrong date.
        wrong_date = _future_date(days=30)

        direct_vm.mock_web(
            r".*",
            {
                "status": 200,
                "body": (
                    f"Flight UA245 was operated on {wrong_date}. "
                    "This page refers to a different date."
                ),
            },
        )

        # If the contract incorrectly invokes the LLM despite the failed
        # deterministic evidence check, this invalid response should cause
        # adjudication to fail instead of returning "undetermined".
        direct_vm.mock_llm(
            r".*",
            "THIS LLM MUST NOT BE CALLED",
        )

        try:
            contract.evaluate_flight(args=[policy_id]).transact()
        finally:
            direct_vm.clear_mocks()

        policy = contract.get_policy(args=[policy_id]).call()

        assert policy["verdict"] == "undetermined"
        assert policy["status"] == "checked"
        assert policy["evaluations"] == 1


class TestSolvency:
    """Steward guarantee: payouts are reserved and unfunded coverage is rejected."""

    def test_unfunded_purchase_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice

        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy(
                args=["UA100", _future_date(), 80]
            ).transact(value=5)

    def test_funded_purchase_within_liquidity_succeeds(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        contract.fund_pool(args=[]).transact(value=100)

        tx = contract.buy_policy(
            args=["UA100", _future_date(), 80]
        ).transact(value=10)

        assert tx_execution_succeeded(tx)
        assert contract.available_liquidity(args=[]).call() == 30

    def test_second_purchase_beyond_liquidity_reverts(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        contract.fund_pool(args=[]).transact(value=100)

        contract.buy_policy(
            args=["UA100", _future_date(), 80]
        ).transact(value=10)

        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy(
                args=["UA200", _future_date(), 50]
            ).transact(value=5)

    def test_settled_on_time_policy_releases_reservation(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        contract.fund_pool(args=[]).transact(value=100)

        flight_date = _future_date(days=2)

        tx = contract.buy_policy(
            args=["UA100", flight_date, 80]
        ).transact(value=10)
        policy_id = tx.return_value

        assert contract.available_liquidity(args=[]).call() == 30

        _advance(direct_vm, datetime.timedelta(days=3))

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        policy = contract.get_policy(args=[policy_id]).call()

        assert policy["status"] == "settled"
        assert policy["verdict"] == "on_time"

        direct_vm.sender = alice

        tx = contract.claim_payout(
            args=[policy_id]
        ).transact()

        assert tx.return_value == 0
        assert contract.available_liquidity(args=[]).call() == 110


class TestLifecycleTerminalStates:
    """Steward guarantee: settled and disputed policies are terminal."""

    def test_disputed_then_refund_then_evaluate_reverts(
        self, contract, direct_vm
    ):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice

        contract.fund_pool(args=[]).transact(value=100)

        flight_date = _future_date(days=2)

        tx = contract.buy_policy(
            args=["UA300", flight_date, 30]
        ).transact(value=5)
        policy_id = tx.return_value

        direct_vm.sender = bob

        _advance(direct_vm, datetime.timedelta(days=3))

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "delayed",
        )

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        policy = contract.get_policy(args=[policy_id]).call()

        assert policy["status"] == "disputed"

        direct_vm.sender = alice

        tx = contract.claim_payout(
            args=[policy_id]
        ).transact()

        assert tx.return_value == 5

        with direct_vm.expect_revert("Policy already settled"):
            _evaluate_with_verdict(
                contract,
                direct_vm,
                policy_id,
                "delayed",
            )

    def test_settled_policy_rejects_further_evaluation_regression(
        self, contract, direct_vm
    ):
        alice = create_account()
        direct_vm.sender = alice

        contract.fund_pool(args=[]).transact(value=100)

        flight_date = _future_date(days=2)

        tx = contract.buy_policy(
            args=["UA400", flight_date, 30]
        ).transact(value=5)
        policy_id = tx.return_value

        _advance(direct_vm, datetime.timedelta(days=3))

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        _evaluate_with_verdict(
            contract,
            direct_vm,
            policy_id,
            "on_time",
        )

        policy = contract.get_policy(args=[policy_id]).call()

        assert policy["status"] == "settled"
        assert policy["confirmations"] == 2

        with direct_vm.expect_revert("Policy already settled"):
            _evaluate_with_verdict(
                contract,
                direct_vm,
                policy_id,
                "on_time",
            )
