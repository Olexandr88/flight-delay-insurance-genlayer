"""
Executable lifecycle tests for FlightDelayInsurance, using GenLayer's
Direct Mode (`genlayer-test` / `gltest`): the contract runs in-memory,
no GenLayer Studio or Docker required.

Setup:
    pip install genlayer-test

Run (from the repository root):
    gltest tests/ -v
"""

import datetime
import sys

import pytest

CONTRACT_PATH = "contracts/flight_delay_insurance.py"

# Every date in this file is computed relative to BASE_DATE, and the
# harness clock is pinned to it at the start of every test.
BASE_DATE = datetime.date.today()


def _date(days=0):
    return (BASE_DATE + datetime.timedelta(days=days)).isoformat()


def _set_day(direct_vm, days=0):
    """
    Pin the harness clock to 00:00 UTC on BASE_DATE + `days`.

    direct_vm.warp() only updates the VM's own timestamp. The GenLayer SDK
    caches the message (including its datetime) when `genlayer.gl` is first
    imported, so the cached copy is patched as well; otherwise the contract
    would keep seeing the old time.
    """
    iso = f"{_date(days)}T00:00:00Z"
    direct_vm.warp(iso)
    gl = sys.modules.get("genlayer.gl")
    raw = getattr(gl, "message_raw", None)
    if raw is not None:
        raw["datetime"] = iso


@pytest.fixture
def contract(direct_vm, direct_deploy):
    deployed = direct_deploy(CONTRACT_PATH)
    _set_day(direct_vm, 0)
    direct_vm.value = 0
    return deployed


def _fund(contract, direct_vm, amount):
    direct_vm.value = amount
    try:
        contract.fund_pool()
    finally:
        direct_vm.value = 0


def _buy(contract, direct_vm, flight_number, flight_date, payout, premium):
    """Buy a policy and return its id (ids are sequential from 0)."""
    direct_vm.value = premium
    try:
        contract.buy_policy(flight_number, flight_date, payout)
    finally:
        direct_vm.value = 0
    return int(contract.total_policies()) - 1


def _evaluate(contract, direct_vm, policy_id, verdict):
    """
    Evaluate a policy with deterministic web and LLM mocks.

    The mocked evidence page contains the exact flight number and flight
    date stored in the policy, so the contract's deterministic
    evidence-authentication step passes and the mocked LLM verdict is
    actually reached.
    """
    policy = contract.get_policy(policy_id)
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
        return contract.evaluate_flight(policy_id)
    finally:
        direct_vm.clear_mocks()


class TestTimingGuards:
    """Steward guarantee: purchase only before departure, evaluation only after."""

    def test_buy_policy_rejects_past_date(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Coverage can only be purchased"):
            _buy(contract, direct_vm, "UA245", _date(-10), 30, 5)

    def test_buy_policy_succeeds_for_future_date(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        policy_id = _buy(contract, direct_vm, "UA245", _date(10), 30, 5)
        assert contract.total_policies() == 1
        assert contract.get_policy(policy_id)["status"] == "pending"

    def test_evaluate_reverts_before_departure(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        policy_id = _buy(contract, direct_vm, "UA245", _date(10), 30, 5)
        with direct_vm.expect_revert("This flight hasn't departed yet"):
            contract.evaluate_flight(policy_id)

    def test_evaluate_succeeds_after_departure(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        policy_id = _buy(contract, direct_vm, "UA245", _date(2), 30, 5)

        _set_day(direct_vm, 3)
        _evaluate(contract, direct_vm, policy_id, "on_time")

        policy = contract.get_policy(policy_id)
        assert policy["status"] == "checked"
        assert policy["verdict"] == "on_time"


class TestEvidenceIsDerivedAndDateBound:
    """
    Steward guarantee: evidence source is derived and bound to both
    flight_number and flight_date.
    """

    def test_status_url_is_derived_from_flight_and_date(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        flight_date = _date(10)
        policy_id = _buy(contract, direct_vm, "UA245", flight_date, 30, 5)

        url = contract.get_policy(policy_id)["status_url"]
        assert "UA245" in url
        assert flight_date in url

    def test_different_dates_produce_different_urls(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        id_a = _buy(contract, direct_vm, "UA245", _date(10), 30, 5)
        id_b = _buy(contract, direct_vm, "UA245", _date(20), 30, 5)

        url_a = contract.get_policy(id_a)["status_url"]
        url_b = contract.get_policy(id_b)["status_url"]
        assert url_a != url_b


class TestEvidenceAuthenticationBeforeAdjudication:
    """
    Steward guarantee: evidence is verified against both flight_number
    and flight_date before LLM adjudication.
    """

    def test_mismatched_evidence_yields_undetermined_without_llm(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)  # pool must cover the payout
        policy_id = _buy(contract, direct_vm, "UA245", _date(2), 30, 5)

        _set_day(direct_vm, 3)

        # Correct flight number, deliberately wrong date.
        wrong_date = _date(200)
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
        # If the contract wrongly reached the LLM despite the failed
        # deterministic check, this non-JSON reply would make adjudication
        # fail instead of yielding "undetermined".
        direct_vm.mock_llm(r".*", "THIS LLM MUST NOT BE CALLED")

        try:
            contract.evaluate_flight(policy_id)
        finally:
            direct_vm.clear_mocks()

        policy = contract.get_policy(policy_id)
        assert policy["verdict"] == "undetermined"
        assert policy["status"] == "checked"
        assert policy["evaluations"] == 1


class TestSolvency:
    """Steward guarantee: payouts are reserved and unfunded coverage is rejected."""

    def test_unfunded_purchase_reverts(self, contract, direct_vm, direct_alice):
        direct_vm.sender = direct_alice
        with direct_vm.expect_revert("Insufficient pool liquidity"):
            _buy(contract, direct_vm, "UA100", _date(10), 80, 5)

    def test_funded_purchase_within_liquidity_succeeds(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        _buy(contract, direct_vm, "UA100", _date(10), 80, 10)

        assert contract.total_policies() == 1
        assert contract.available_liquidity() == 30  # 110 - 80

    def test_second_purchase_beyond_liquidity_reverts(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        _buy(contract, direct_vm, "UA100", _date(10), 80, 10)

        with direct_vm.expect_revert("Insufficient pool liquidity"):
            _buy(contract, direct_vm, "UA200", _date(10), 50, 5)

    def test_settled_on_time_policy_releases_reservation(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        policy_id = _buy(contract, direct_vm, "UA100", _date(2), 80, 10)
        assert contract.available_liquidity() == 30

        _set_day(direct_vm, 3)
        _evaluate(contract, direct_vm, policy_id, "on_time")
        _evaluate(contract, direct_vm, policy_id, "on_time")

        policy = contract.get_policy(policy_id)
        assert policy["status"] == "settled"
        assert policy["verdict"] == "on_time"

        contract.claim_payout(policy_id)

        assert contract.get_policy(policy_id)["claimed"] is True
        assert contract.get_pool_balance() == 110  # premium stays in the pool
        assert contract.available_liquidity() == 110

    def test_delayed_settled_policy_pays_full_payout(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        policy_id = _buy(contract, direct_vm, "UA500", _date(2), 80, 10)

        _set_day(direct_vm, 3)
        _evaluate(contract, direct_vm, policy_id, "delayed")
        _evaluate(contract, direct_vm, policy_id, "delayed")

        policy = contract.get_policy(policy_id)
        assert policy["status"] == "settled"
        assert policy["verdict"] == "delayed"

        contract.claim_payout(policy_id)

        assert contract.get_policy(policy_id)["claimed"] is True
        assert contract.get_pool_balance() == 30  # 110 - 80 paid out
        assert contract.available_liquidity() == 30

        with direct_vm.expect_revert("Already claimed"):
            contract.claim_payout(policy_id)


class TestLifecycleTerminalStates:
    """Steward guarantee: settled and disputed policies are terminal."""

    def test_disputed_then_refund_then_evaluate_reverts(
        self, contract, direct_vm, direct_alice, direct_bob
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        policy_id = _buy(contract, direct_vm, "UA300", _date(2), 30, 5)

        # Anyone may trigger evaluations; use a different account.
        direct_vm.sender = direct_bob
        _set_day(direct_vm, 3)
        _evaluate(contract, direct_vm, policy_id, "delayed")
        _evaluate(contract, direct_vm, policy_id, "on_time")

        assert contract.get_policy(policy_id)["status"] == "disputed"

        # The policyholder gets the premium (5) back.
        direct_vm.sender = direct_alice
        contract.claim_payout(policy_id)
        assert contract.get_policy(policy_id)["claimed"] is True
        assert contract.get_pool_balance() == 100  # 105 - 5 refunded

        with direct_vm.expect_revert("Policy already settled"):
            _evaluate(contract, direct_vm, policy_id, "delayed")

    def test_settled_policy_rejects_further_evaluation_regression(
        self, contract, direct_vm, direct_alice
    ):
        direct_vm.sender = direct_alice
        _fund(contract, direct_vm, 100)
        policy_id = _buy(contract, direct_vm, "UA400", _date(2), 30, 5)

        _set_day(direct_vm, 3)
        _evaluate(contract, direct_vm, policy_id, "on_time")
        _evaluate(contract, direct_vm, policy_id, "on_time")

        policy = contract.get_policy(policy_id)
        assert policy["status"] == "settled"
        assert policy["confirmations"] == 2

        with direct_vm.expect_revert("Policy already settled"):
            _evaluate(contract, direct_vm, policy_id, "on_time")
