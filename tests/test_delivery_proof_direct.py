import json
import pytest

DEFAULT_TRACKING_URL = "https://tracking.carrier.global/manifest/MNF-2026-9901"
DEFAULT_TELEMETRY_URL = "https://iot.coldchain.io/telemetry/MNF-2026-9901"
DEFAULT_INSPECTION_URL = "https://customs.gov.inspection/cert/CERT-8812"

DEFAULT_TRACKING_PAYLOAD = {
    "manifest_id": "MNF-2026-9901",
    "carrier_code": "GLOBAL_COLD_EXPRESS",
    "status": "DELIVERED",
    "destination": "JFK-COLD-HUB",
    "pickup_time": "2026-10-18T08:00:00Z",
    "delivery_time": "2026-10-20T12:00:00Z",
    "actual_transit_hours": 52,
    "pod_signed_by": "J. Doe - Receiving Mgr",
}

DEFAULT_TELEMETRY_PAYLOAD = {
    "device_id": "SENS-TEMP-9981",
    "sample_rate_sec": 300,
    "temperature_unit": "CELSIUS",
    "min_temp": 3.2,
    "max_temp": 11.0,
    "avg_temp": 5.4,
    "tamper_detected": False,
    "battery_ok": True,
}

DEFAULT_INSPECTION_PAYLOAD = {
    "certificate_id": "CERT-8812",
    "inspector": "Federal Port Health Authority",
    "finding": "Continuous independent certified logger confirmed cargo integrity; secondary sensor calibration offset explains primary spike.",
    "calibrated_max_temp_c": 6.8,
    "package_integrity": "INTACT",
    "cargo_status": "RELEASED_PRIME",
}


@pytest.fixture(autouse=True)
def default_mocks(direct_vm):
    """Register standard web mocks before every test."""
    direct_vm.mock_web(
        r".*tracking\.carrier\.global.*",
        {"status": 200, "body": json.dumps(DEFAULT_TRACKING_PAYLOAD)},
    )
    direct_vm.mock_web(
        r".*iot\.coldchain\.io.*",
        {"status": 200, "body": json.dumps(DEFAULT_TELEMETRY_PAYLOAD)},
    )
    direct_vm.mock_web(
        r".*customs\.gov\.inspection.*",
        {"status": 200, "body": json.dumps(DEFAULT_INSPECTION_PAYLOAD)},
    )


def test_deposit_and_native_withdrawal(direct_vm, direct_deploy, direct_alice, direct_bob):
    """
    Test 1: Value-Backed Payable Escrow & Native Withdrawal (CEI)
    - Verifies deposit_funds() requires msg.value > 0.
    - Verifies balance tracking.
    - Verifies withdraw() guards against zero amount and insufficient balance.
    - Verifies native transfer path and post-withdrawal balance consistency.
    """
    timelock_keeper = "0x" + direct_bob.hex()
    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper)

    # Calling deposit_funds with 0 value reverts
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Must attach non-zero value to deposit"):
        contract.deposit_funds()

    # Calling deposit_funds with 5 ETH attaches real native value
    deposit_amount = 5 * 10**18
    direct_vm.value = deposit_amount
    new_bal = contract.deposit_funds()
    assert new_bal == deposit_amount
    assert contract.get_balance(direct_alice) == deposit_amount

    # Calling withdraw with 0 reverts
    with direct_vm.expect_revert("Withdrawal amount must be greater than zero"):
        contract.withdraw(0)

    # Calling withdraw exceeding balance reverts
    with direct_vm.expect_revert("Insufficient balance for withdrawal"):
        contract.withdraw(deposit_amount + 1)

    # Calling valid partial withdraw updates balance and records withdrawn total
    withdraw_amount = 2 * 10**18
    res = contract.withdraw(withdraw_amount)
    assert str(res["recipient"]).lower() == ("0x" + direct_alice.hex()).lower()
    assert res["amount"] == withdraw_amount
    assert res["remaining_balance"] == 3 * 10**18
    assert contract.get_balance(direct_alice) == 3 * 10**18
    assert contract.get_withdrawn_total(direct_alice) == withdraw_amount


def test_create_shipment_and_carrier_bond_value_backed(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Test 2: Value-Backed Shipment Creation & Carrier Performance Bond Staking
    - Verifies input validation (carrier != buyer, non-zero SLAs).
    - Verifies native value attachment on create_shipment & balance deductions.
    - Verifies access control on carrier bond staking (carrier-only).
    - Verifies transition from CREATED to IN_TRANSIT.
    """
    timelock_keeper = "0x" + direct_charlie.hex()
    buyer = direct_alice
    carrier = direct_bob
    carrier_addr = "0x" + direct_bob.hex()

    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper)

    freight_payout = 3 * 10**18
    carrier_bond = 1 * 10**18

    # Cannot set buyer as carrier
    direct_vm.sender = buyer
    direct_vm.value = freight_payout
    with direct_vm.expect_revert("Carrier cannot be buyer"):
        contract.create_shipment(
            "0x" + buyer.hex(),
            "MNF-001",
            "JFK",
            freight_payout,
            carrier_bond,
            48,
            200,
            8,
            3000,
            10,
        )

    # Cannot create without funding escrow
    direct_vm.value = 0
    with direct_vm.expect_revert("Insufficient balance for freight payout escrow"):
        contract.create_shipment(
            carrier_addr,
            "MNF-001",
            "JFK",
            freight_payout,
            carrier_bond,
            48,
            200,
            8,
            3000,
            10,
        )

    # Create shipment with attached freight payout native value
    direct_vm.value = freight_payout
    shipment_id = contract.create_shipment(
        carrier_addr,
        "MNF-001",
        "JFK",
        freight_payout,
        carrier_bond,
        48,
        200,
        8,
        3000,
        10,
    )
    assert shipment_id == 1

    s = contract.get_shipment(1)
    assert s["status"] == "CREATED"
    assert s["freight_payout"] == freight_payout
    assert s["required_carrier_bond"] == carrier_bond
    assert s["carrier_bond_locked"] == 0

    state = contract.get_contract_state()
    assert state["total_escrow_locked"] == freight_payout

    # Non-carrier (buyer) cannot stake bond
    direct_vm.sender = buyer
    direct_vm.value = carrier_bond
    with direct_vm.expect_revert("Only designated carrier can stake bond"):
        contract.stake_carrier_bond(1)

    # Carrier cannot stake without funds
    direct_vm.sender = carrier
    direct_vm.value = 0
    with direct_vm.expect_revert("Insufficient balance for carrier bond"):
        contract.stake_carrier_bond(1)

    # Carrier stakes bond with attached native value
    direct_vm.value = carrier_bond
    contract.stake_carrier_bond(1)

    s2 = contract.get_shipment(1)
    assert s2["status"] == "IN_TRANSIT"
    assert s2["carrier_bond_locked"] == carrier_bond

    state2 = contract.get_contract_state()
    assert state2["total_escrow_locked"] == freight_payout + carrier_bond


def test_cold_chain_and_delay_sla_breach_partial_and_settlement(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Test 3: Cold-Chain & Delay SLA Penalty Math (SLA_BREACH_PARTIAL) & Settlement
    - Carrier submits proof URLs.
    - Non-deterministic oracle extracts 52h transit (4h late vs 48h SLA) and 11C temp (excursion vs 8C ceiling).
    - SLA Penalty:
        delay penalty = 4h * 200 bps = 800 bps
        temp penalty = 3000 bps
        total penalty = 3800 bps (< 5000 bps)
        carrier payout = 6200 bps (62%)
        bond slash = 0 bps (0%)
    - Verifies validator consensus.
    - Verifies mutual counterparty waiver & settlement math.
    """
    timelock_keeper = "0x" + direct_charlie.hex()
    buyer = direct_alice
    carrier = direct_bob
    carrier_addr = "0x" + direct_bob.hex()

    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper)

    freight = 10000
    bond = 5000

    # Fund & create shipment
    direct_vm.sender = buyer
    direct_vm.value = freight
    shipment_id = contract.create_shipment(
        carrier_addr,
        "MNF-2026-9901",
        "JFK-COLD-HUB",
        freight,
        bond,
        48,  # max_transit_hours
        200, # delay_penalty_bps_per_hour (2%/hr)
        8,   # max_temp_celsius (8 C)
        3000,# temp_breach_penalty_bps (30%)
        5,   # dispute_window_blocks
    )

    # Carrier stakes bond
    direct_vm.sender = carrier
    direct_vm.value = bond
    contract.stake_carrier_bond(shipment_id)

    # Carrier submits delivery proof HTTPS URLs
    contract.submit_delivery_proof(shipment_id, DEFAULT_TRACKING_URL, DEFAULT_TELEMETRY_URL)

    # Mock LLM oracle response for delivery & telemetry
    oracle_response = {
        "delivered": True,
        "destination_matched": True,
        "actual_transit_hours": 52,
        "observed_max_temp_c": 11,
        "tamper_or_damage": False,
        "summary": "Delivered with 4-hour delay and minor cold-chain excursion to 11C.",
    }
    direct_vm.mock_llm(r".*carrier tracking.*", json.dumps(oracle_response))

    # Trigger delivery and SLA verification
    eval_res = contract.verify_delivery_and_sla(shipment_id)
    assert eval_res["outcome"] == "SLA_BREACH_PARTIAL"
    assert eval_res["carrier_payout_bps"] == 6200
    assert eval_res["bond_slash_bps"] == 0
    assert eval_res["temp_excursion"] is True

    # Multi-validator consensus verification
    assert direct_vm.run_validator() is True

    s = contract.get_shipment(shipment_id)
    assert s["status"] == "PROVISIONAL"
    assert s["carrier_payout_bps"] == 6200

    # Mutual waiver allows early finalization for partial verdicts
    direct_vm.sender = buyer
    assert contract.waive_dispute_window(shipment_id) == "PROVISIONAL"  # only buyer waived so far

    direct_vm.sender = carrier
    assert contract.waive_dispute_window(shipment_id) == "FINALIZED"    # both waived -> FINALIZED!

    # Settle shipment
    payout = contract.settle_shipment(shipment_id)
    assert payout["status"] == "SETTLED"
    assert payout["carrier_freight_share"] == 6200 # 62% of 10000
    assert payout["buyer_freight_rebate"] == 3800  # 38% of 10000
    assert payout["slashed_bond_to_buyer"] == 0    # 0 bond slashed
    assert payout["returned_bond_to_carrier"] == 5000 # 100% bond returned

    assert contract.get_balance(carrier) == 6200 + 5000
    assert contract.get_balance(buyer) == 3800


def test_appellate_inspection_challenge_and_resolution(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Test 4: Multi-Source Appellate Re-evaluation
    - Initial evaluation yields partial penalty.
    - Carrier challenges SLA verdict during dispute window with official customs calibration URL.
    - Status transitions PROVISIONAL -> DISPUTED.
    - Appellate evaluation re-evaluates all 3 sources (tracking, telemetry, inspection).
    - Outcome overturned to ON_TIME_INTACT.
    - Settle awards full freight payout + returned bond to carrier.
    """
    timelock_keeper = "0x" + direct_charlie.hex()
    buyer = direct_alice
    carrier = direct_bob
    carrier_addr = "0x" + direct_bob.hex()

    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper)

    freight = 10000
    bond = 5000

    direct_vm.sender = buyer
    direct_vm.value = freight
    shipment_id = contract.create_shipment(
        carrier_addr, "MNF-2026-9901", "JFK-COLD-HUB", freight, bond, 48, 200, 8, 3000, 10
    )

    direct_vm.sender = carrier
    direct_vm.value = bond
    contract.stake_carrier_bond(shipment_id)

    contract.submit_delivery_proof(shipment_id, DEFAULT_TRACKING_URL, DEFAULT_TELEMETRY_URL)

    # Initial flawed evaluation
    initial_response = {
        "delivered": True,
        "destination_matched": True,
        "actual_transit_hours": 52,
        "observed_max_temp_c": 12,
        "tamper_or_damage": False,
        "summary": "Initial sensor reading indicated excursion.",
    }
    direct_vm.mock_llm(r".*carrier tracking.*", json.dumps(initial_response))
    contract.verify_delivery_and_sla(shipment_id)
    assert contract.get_shipment(shipment_id)["status"] == "PROVISIONAL"

    # Carrier challenges with independent customs inspection URL
    contract.challenge_sla_verdict(
        shipment_id,
        DEFAULT_INSPECTION_URL,
        "Port health authority certified cargo logger calibration offset; transit was on-time and cold-chain intact.",
    )
    assert contract.get_shipment(shipment_id)["status"] == "DISPUTED"

    # Appellate tribunal LLM re-evaluates with inspection certificate
    appellate_response = {
        "delivered": True,
        "destination_matched": True,
        "actual_transit_hours": 48,
        "observed_max_temp_c": 6,
        "tamper_or_damage": False,
        "summary": "Appellate certificate verified intact delivery and calibrated temperature within 8C ceiling.",
    }
    direct_vm.mock_llm(r".*appellate logistics tribunal.*", json.dumps(appellate_response))

    resolved = contract.resolve_sla_dispute(shipment_id)
    assert resolved["outcome"] == "ON_TIME_INTACT"
    assert resolved["carrier_payout_bps"] == 10000
    assert resolved["bond_slash_bps"] == 0

    assert direct_vm.run_validator() is True

    s = contract.get_shipment(shipment_id)
    assert s["status"] == "FINALIZED"
    assert s["delivery_outcome"] == "ON_TIME_INTACT"

    # Settle
    payout = contract.settle_shipment(shipment_id)
    assert payout["carrier_freight_share"] == 10000
    assert payout["buyer_freight_rebate"] == 0
    assert payout["returned_bond_to_carrier"] == 5000


def test_catastrophic_damaged_or_lost_outcome(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Test 5: Catastrophic Loss / Damage Verdict
    - Cargo tamper/damage detected.
    - Outcome: DAMAGED_OR_LOST.
    - Payout: 0 bps to carrier.
    - Bond slash: 10000 bps (100% slashed to buyer).
    - Carrier unilateral concession / waiver immediately finalizes.
    - Buyer receives full freight rebate AND full carrier bond.
    """
    timelock_keeper = "0x" + direct_charlie.hex()
    buyer = direct_alice
    carrier = direct_bob
    carrier_addr = "0x" + direct_bob.hex()

    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper)

    freight = 20000
    bond = 10000

    direct_vm.sender = buyer
    direct_vm.value = freight
    shipment_id = contract.create_shipment(
        carrier_addr, "MNF-FAIL-001", "JFK-COLD-HUB", freight, bond, 48, 200, 8, 3000, 10
    )

    direct_vm.sender = carrier
    direct_vm.value = bond
    contract.stake_carrier_bond(shipment_id)

    contract.submit_delivery_proof(shipment_id, DEFAULT_TRACKING_URL, DEFAULT_TELEMETRY_URL)

    # Catastrophic breach
    catastrophic_response = {
        "delivered": False,
        "destination_matched": False,
        "actual_transit_hours": 90,
        "observed_max_temp_c": 35,
        "tamper_or_damage": True,
        "summary": "Container breached and looted; total cargo destruction.",
    }
    direct_vm.mock_llm(r".*carrier tracking.*", json.dumps(catastrophic_response))

    eval_res = contract.verify_delivery_and_sla(shipment_id)
    assert eval_res["outcome"] == "DAMAGED_OR_LOST"
    assert eval_res["carrier_payout_bps"] == 0
    assert eval_res["bond_slash_bps"] == 10000

    assert direct_vm.run_validator() is True

    # Carrier unilaterally concedes / waives dispute window
    direct_vm.sender = carrier
    fin_status = contract.waive_dispute_window(shipment_id)
    assert fin_status == "FINALIZED"

    # Settlement
    payout = contract.settle_shipment(shipment_id)
    assert payout["carrier_freight_share"] == 0
    assert payout["buyer_freight_rebate"] == freight
    assert payout["slashed_bond_to_buyer"] == bond
    assert payout["returned_bond_to_carrier"] == 0

    assert contract.get_balance(buyer) == freight + bond
    assert contract.get_balance(carrier) == 0


def test_timelock_keeper_clock_guards_and_validator_disagreement(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """
    Test 6: Clock & Partisan Keeper Restrictions + Validator Disagreement
    - Restricted keeper: Only timelock_keeper can advance_epoch().
    - Bounds check: steps must be between 1 and 50.
    - Partisan guard: Reverts if keeper is party (buyer/carrier) to a PROVISIONAL shipment.
    - Validator disagreement: Validator independently rejects conflicting leader result.
    """
    timelock_keeper_addr = "0x" + direct_alice.hex()
    contract = direct_deploy("contracts/delivery_proof.py", timelock_keeper_addr)

    # Non-keeper cannot advance epoch
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only timelock keeper can advance epoch"):
        contract.advance_epoch(5)

    # Keeper cannot advance invalid steps
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Steps must be between 1 and 50"):
        contract.advance_epoch(0)

    with direct_vm.expect_revert("Steps must be between 1 and 50"):
        contract.advance_epoch(51)

    # Valid step
    epoch = contract.advance_epoch(10)
    assert epoch == 10

    # Create a shipment where timelock_keeper (alice) is the buyer
    freight = 1000
    bond = 500
    direct_vm.value = freight
    shipment_id = contract.create_shipment(
        "0x" + direct_bob.hex(), "MNF-PARTISAN", "ORD", freight, bond, 24, 100, 8, 2000, 5
    )

    direct_vm.sender = direct_bob
    direct_vm.value = bond
    contract.stake_carrier_bond(shipment_id)
    contract.submit_delivery_proof(shipment_id, DEFAULT_TRACKING_URL, DEFAULT_TELEMETRY_URL)

    # Leader produces valid delivery response
    leader_response = {
        "delivered": True,
        "destination_matched": True,
        "actual_transit_hours": 20,
        "observed_max_temp_c": 5,
        "tamper_or_damage": False,
        "summary": "On time delivery.",
    }
    direct_vm.mock_llm(r".*carrier tracking.*", json.dumps(leader_response))
    contract.verify_delivery_and_sla(shipment_id)
    assert contract.get_shipment(shipment_id)["status"] == "PROVISIONAL"

    # Partisan Guard: Alice (timelock_keeper) is the buyer on this PROVISIONAL shipment!
    # advance_epoch MUST revert!
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Partisan keeper cannot advance epoch while party to provisional shipment"):
        contract.advance_epoch(1)

    # Validator disagreement test:
    # If the validator independently evaluates the shipment as DAMAGED_OR_LOST instead of ON_TIME_INTACT
    conflicting_val_response = {
        "delivered": False,
        "destination_matched": False,
        "actual_transit_hours": 20,
        "observed_max_temp_c": 5,
        "tamper_or_damage": True,
        "summary": "Validator detected tamper.",
    }
    direct_vm.clear_mocks()
    direct_vm.mock_web(r".*tracking\.carrier\.global.*", {"status": 200, "body": json.dumps(DEFAULT_TRACKING_PAYLOAD)})
    direct_vm.mock_web(r".*iot\.coldchain\.io.*", {"status": 200, "body": json.dumps(DEFAULT_TELEMETRY_PAYLOAD)})
    direct_vm.mock_llm(r".*", json.dumps(conflicting_val_response))

    # Validator runs and MUST reject (return False)
    val_passed = direct_vm.run_validator()
    assert val_passed is False, "Validator must reject when substantive outcome disagrees"
