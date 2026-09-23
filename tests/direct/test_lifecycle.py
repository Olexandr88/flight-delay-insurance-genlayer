"""
Executable lifecycle tests for PredictionAdjudicator, using GenLayer's
official testing suite (`genlayer-test` / `gltest`) in Direct Mode.

Setup:
    pip install genlayer-test

Run:
    gltest tests/ -v
"""

import datetime

import pytest
from gltest import get_contract_factory, create_account
from gltest.assertions import tx_execution_succeeded


@pytest.fixture
def contract():
    factory = get_contract_factory("PredictionAdjudicator")
    return factory.deploy()


def _advance(direct_vm, delta):
    """
    Moves the harness's clock forward by `delta`. Uses direct_vm.warp(),
    the documented Direct Mode time-travel cheatcode. Some gltest
    releases don't propagate warp() into the datetime a contract sees via
    Python's datetime.datetime.now() inside GenVM execution, so this also
    patches the underlying message datetime directly as a fallback.
    """
    direct_vm.warp(delta)
    try:
        current = direct_vm.message_raw["datetime"]
        direct_vm.message_raw["datetime"] = current + delta
    except (AttributeError, KeyError, TypeError):
        pass


def _resolve_with_verdict(contract, direct_vm, claim_id, verdict):
    direct_vm.mock_web(r".*", {"status": 200, "body": "mock evidence page"})
    direct_vm.mock_llm(
        r".*",
        f'{{"verdict": "{verdict}", "reasoning": "mocked evidence supports this verdict"}}',
    )
    tx = contract.resolve(args=[claim_id]).transact()
    direct_vm.clear_mocks()
    return tx


class TestResolvedClaimRejectsNewStakes:
    """Steward guarantee: staking is pending-only."""

    def test_stake_succeeds_while_pending(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=[
                "GenLayer testnet processed 1M+ transactions in August 2026",
                "https://example.com/evidence",
                "The page must state the transaction count explicitly",
            ]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        tx = contract.stake_true(args=[claim_id]).transact(value=10)
        assert tx_execution_succeeded(tx)

    def test_stake_reverts_once_resolved(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=[
                "GenLayer testnet processed 1M+ transactions in August 2026",
                "https://example.com/evidence",
                "The page must state the transaction count explicitly",
            ]
        ).transact()
        claim_id = tx.return_value

        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        claim = contract.get_claim(args=[claim_id]).call()
        assert claim["status"] == "resolved"

        direct_vm.sender = bob
        with direct_vm.expect_revert("Staking is only open while a claim is pending"):
            contract.stake_true(args=[claim_id]).transact(value=10)
        with direct_vm.expect_revert("Staking is only open while a claim is pending"):
            contract.stake_false(args=[claim_id]).transact(value=10)


class TestDisputedClaimCannotFinalizeAfterRefund:
    """Steward guarantee: disputed is terminal, even after a refund."""

    def test_dispute_then_refund_then_resolve_reverts(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A contested claim", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        contract.stake_true(args=[claim_id]).transact(value=10)

        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        _resolve_with_verdict(contract, direct_vm, claim_id, "false")
        claim = contract.get_claim(args=[claim_id]).call()
        assert claim["status"] == "disputed"

        direct_vm.sender = bob
        tx = contract.claim_winnings(args=[claim_id]).transact()
        assert tx.return_value == 10

        with direct_vm.expect_revert("Claim already settled"):
            _resolve_with_verdict(contract, direct_vm, claim_id, "true")


class TestFinalizedClaimRejectsResolveRegression:
    """Original guarantee from the first review round, still enforced."""

    def test_finalized_claim_rejects_resolve(self, contract, direct_vm):
        alice = create_account()
        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A claim that will be finalized", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        _resolve_with_verdict(contract, direct_vm, claim_id, "true")

        claim = contract.get_claim(args=[claim_id]).call()
        assert claim["status"] == "finalized"
        assert claim["confirmations"] == 2

        with direct_vm.expect_revert("Claim already settled"):
            _resolve_with_verdict(contract, direct_vm, claim_id, "true")


class TestZeroPoolSideRefundsInsteadOfDividingByZero:
    """
    Steward guarantee: if a claim finalizes on a verdict nobody staked
    on, claim_winnings() refunds stakers instead of dividing by zero.
    """

    def test_finalized_true_with_zero_true_pool_refunds(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A claim where everyone bet wrong", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        contract.stake_false(args=[claim_id]).transact(value=15)

        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        claim = contract.get_claim(args=[claim_id]).call()
        assert claim["status"] == "finalized"
        assert claim["verdict"] == "true"
        assert claim["pool_true"] == 0

        direct_vm.sender = bob
        tx = contract.claim_winnings(args=[claim_id]).transact()
        assert tx.return_value == 15


class TestReclaimStakeForStuckClaims:
    """
    Steward guarantee: if a claim never reaches a terminal state,
    stakers can recover their own GEN after RECLAIM_AFTER has elapsed,
    instead of it being locked forever.
    """

    def test_reclaim_reverts_before_deadline(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A claim nobody will resolve in time", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        contract.stake_true(args=[claim_id]).transact(value=20)

        with direct_vm.expect_revert("reclaim deadline hasn't passed yet"):
            contract.reclaim_stake(args=[claim_id]).transact()

    def test_reclaim_reverts_on_finalized_claim(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A claim that gets finalized normally", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        contract.stake_true(args=[claim_id]).transact(value=20)

        _resolve_with_verdict(contract, direct_vm, claim_id, "true")
        _resolve_with_verdict(contract, direct_vm, claim_id, "true")

        direct_vm.sender = bob
        with direct_vm.expect_revert("already reached a terminal state"):
            contract.reclaim_stake(args=[claim_id]).transact()

    def test_reclaim_succeeds_after_deadline(self, contract, direct_vm):
        alice = create_account()
        bob = create_account()

        direct_vm.sender = alice
        tx = contract.submit_claim(
            args=["A claim nobody ever finishes resolving", "https://example.com/evidence", "Some criteria"]
        ).transact()
        claim_id = tx.return_value

        direct_vm.sender = bob
        contract.stake_true(args=[claim_id]).transact(value=20)

        # Advance the harness clock past RECLAIM_AFTER (7 days) without
        # ever resolving the claim to a terminal state.
        _advance(direct_vm, datetime.timedelta(days=8))

        tx = contract.reclaim_stake(args=[claim_id]).transact()
        assert tx.return_value == 20  # full stake recovered

        with direct_vm.expect_revert("Already claimed"):
            contract.reclaim_stake(args=[claim_id]).transact()
