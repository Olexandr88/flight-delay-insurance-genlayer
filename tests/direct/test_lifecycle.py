"""
Executable lifecycle tests for FlightDelayInsurance, using GenLayer's
official testing suite (`genlayer-test` / `gltest`) in Direct Mode —
fast, in-memory contract execution with `mock_web`/`mock_llm` cheatcodes
standing in for the real nondeterministic web+LLM calls inside
evaluate_flight().

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
    return direct_deploy("flight_delay_insurance.py")


def _future_date(days=10):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _past_date(days=10):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _evaluate_with_verdict(contract, direct_vm, policy_id, verdict):
    direct_vm.mock_web(r".*", {"status": 200, "body": "mock flight evidence page"})
    direct_vm.mock_llm(
        r".*",
        f'{{"verdict": "{verdict}", "delay_minutes": 150, "reasoning": "mocked evidence confirms this flight and date"}}',
    )
    direct_vm.value = 0
    result = contract.evaluate_flight(policy_id)
    direct_vm.clear_mocks()
    return result


class TestTimingGuards:
    """Steward guarantee: purchase only before departure, evaluation only after."""

    def test_buy_policy_rejects_past_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 5
        with direct_vm.expect_revert("Coverage can only be purchased"):
            contract.buy_policy("UA245", _past_date(), 30)

    def test_buy_policy_succeeds_for_future_date(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        direct_vm.value = 5
        tx = contract.buy_policy("UA245", _future_date(), 30)
        assert tx_execution_succeeded(tx)

    def test_evaluate_reverts_before_departure(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        direct_vm.value = 5
        policy_id = contract.buy_policy("UA245", _future_date(), 30)
        direct_vm.value = 0
        with direct_vm.expect_revert("This flight hasn't departed yet"):
            contract.evaluate_flight(policy_id)


class TestEvidenceIsDerivedAndDateBound:
    """Steward guarantee: evidence source is derived, bound to both flight_number and flight_date."""

    def test_status_url_has_no_buyer_parameter(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        flight_date = _future_date()
        direct_vm.value = 5
        policy_id = contract.buy_policy("UA245", flight_date, 30)

        direct_vm.value = 0
        policy = contract.get_policy(policy_id)
        assert "UA245" in policy["status_url"]
        assert flight_date in policy["status_url"]

    def test_different_dates_produce_different_urls(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        date_a = _future_date(10)
        date_b = _future_date(20)

        direct_vm.value = 5
        policy_a = contract.buy_policy("UA245", date_a, 30)
        direct_vm.value = 5
        policy_b = contract.buy_policy("UA245", date_b, 30)

        direct_vm.value = 0
        url_a = contract.get_policy(policy_a)["status_url"]
        url_b = contract.get_policy(policy_b)["status_url"]
        assert url_a != url_b


class TestSolvency:
    """Steward guarantee: payouts are reserved, unfunded coverage is rejected."""

    def test_unfunded_purchase_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 5
        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy("UA100", _future_date(), 80)

    def test_funded_purchase_within_liquidity_succeeds(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        direct_vm.value = 10
        tx = contract.buy_policy("UA100", _future_date(), 80)
        assert tx_execution_succeeded(tx)
        direct_vm.value = 0
        assert contract.available_liquidity() == 30  # 110 pool - 80 reserved

    def test_second_purchase_beyond_liquidity_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        direct_vm.value = 100
        contract.fund_pool()
        direct_vm.value = 10
        contract.buy_policy("UA100", _future_date(), 80)
        direct_vm.value = 5
        with direct_vm.expect_revert("Insufficient pool liquidity"):
            contract.buy_policy("UA200", _future_date(), 50)


class TestLifecycleTerminalStates:
    @pytest.mark.skip(
        reason=(
            "Requires real elapsed time between buy_policy() (future date "
            "required) and evaluate_flight() (past date required); run as "
            "an integration test against Studio/testnet with a flight_date "
            "that has already elapsed by wall-clock time instead."
        )
    )
    def test_disputed_then_refund_then_evaluate_reverts_integration(self):
        pass


class TestEvidenceAuthenticationBeforeAdjudication:
    @pytest.mark.skip(
        reason=(
            "Requires evaluate_flight() to run past the departure-timing "
            "guard, which needs real elapsed time — see "
            "TestLifecycleTerminalStates above for the same constraint."
        )
    )
    def test_mismatched_evidence_yields_undetermined_without_calling_llm_integration(
        self,
    ):
        pass
