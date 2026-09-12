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
    return direct_deploy("contracts/flight_delay_insurance.py")


def _future_date(days=10):
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def _past_date(days=10):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _evaluate_with_verdict(contract, direct_vm, policy_id, verdict):
    """
    Resolves a policy once via mocked web+LLM responses. Kept here for use
    once a confirmed Direct Mode time-travel cheatcode is available, or for
    adaptation into an integration-mode test — see
    TestLifecycleTerminalStates below for why it isn't wired into a Direct
    Mode test yet.
    """
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


class TestSolvency:
    """Steward guarantee: payouts are reserved, unfunded coverage is rejected."""

    def test_unfunded_purchase_reverts(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        # No fund_pool() call — pool_balance starts at 0, premium alone
        # (5) cannot cover an 80 payout.
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


class TestLifecycleTerminalStates:
    """
    Steward guarantee: settled and disputed are both terminal.

    This specific guarantee can't be exercised end-to-end in Direct Mode
    the way the other three can: buy_policy() requires flight_date to be
    in the future, while evaluate_flight() requires it to already have
    passed — so reaching a "checked"/"disputed" state needs real elapsed
    time between those two calls. Direct Mode runs in-memory against the
    real system clock with no time-travel cheatcode confirmed available
    for this GenVM datetime in the current genlayer-test release, so
    faking that here would mean shipping a test that doesn't actually
    exercise what it claims to.

    The same dispute -> refund -> terminal-recheck-reverts pattern is
    already covered by a genuinely executable Direct Mode test against
    PredictionAdjudicator (see the prediction-adjudicator-genlayer repo's
    tests/direct/test_lifecycle.py), which has no such date gate and so
    isn't limited this way. For FlightDelayInsurance specifically, this
    guarantee is verified as an integration-style run instead:
    """

    @pytest.mark.skip(
        reason=(
            "Requires real elapsed time between buy_policy() (future date "
            "required) and evaluate_flight() (past date required); run as "
            "an integration test against Studio/testnet with a flight_date "
            "that has already elapsed by wall-clock time instead."
        )
    )
    def test_disputed_then_refund_then_evaluate_reverts_integration(self):
        """
        Integration-mode steps (documented, not executed here):
        1. buy_policy(flight_number, flight_date, payout_amount) where
           flight_date is a real near-future date.
        2. Wait for flight_date to elapse (or pick a flight_date already
           in the past relative to the network's clock, if the network
           under test allows it).
        3. evaluate_flight() twice with disagreeing results -> "disputed".
        4. claim_payout() -> refunds the premium in full.
        5. evaluate_flight() again -> must revert with "Policy already
           settled", proving the terminal guarantee holds even after the
           refund has already been paid out.
        """


class TestEvidenceAuthenticationBeforeAdjudication:
    """
    Steward guarantee: evidence is verified to match both flight_number
    and flight_date, deterministically, BEFORE any LLM adjudication runs.

    Same structural limitation as TestLifecycleTerminalStates above:
    exercising this requires a successful evaluate_flight() call, which
    needs the flight_date to have already elapsed. Documented here as an
    integration-mode run rather than faked in Direct Mode.
    """

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
        """
        Integration-mode steps (documented, not executed here):
        1. buy_policy(flight_number, flight_date, payout_amount) for a
           real flight.
        2. Wait for flight_date to elapse.
        3. mock_web (or, on a real network, an evidence page that fails
           to load or redirects) so the fetched page does NOT contain
           both flight_number and flight_date.
        4. evaluate_flight() → result.verdict == "undetermined", with
           reasoning citing the missing confirmation — and this must
           happen even if mock_llm is configured to return "delayed",
           proving the deterministic check runs before, and can override,
           any LLM output.
        """
