import json
import os
import subprocess
import time
from eth_account import Account
from genlayer_py import create_client, studionet
from web3 import Web3

CONTRACT_ADDRESS = "0x2FcD9bbbad3d37b39BC440d9d0cAe34Ac9f9d6f8"

def get_deployer_account():
    cmd = [
        "node", "-e",
        "const keytar = require('C:/Users/NO GO NO/AppData/Roaming/npm/node_modules/genlayer/node_modules/keytar'); "
        "keytar.getPassword('genlayer-cli', 'account:ace-deployer').then(p => process.stdout.write(p));"
    ]
    pk = subprocess.check_output(cmd, text=True).strip()
    return Account.from_key(pk)

def patch_client_for_rate_limits(client):
    orig_wait = client.w3.eth.wait_for_transaction_receipt
    def polite_wait(tx_hash, timeout=180, poll_latency=5):
        # Poll at 5 second intervals to respect Cloudflare rate limits
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                receipt = client.w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt
            except Exception:
                time.sleep(3)
            time.sleep(poll_latency)
        return orig_wait(tx_hash, timeout=10, poll_latency=5)
    client.w3.eth.wait_for_transaction_receipt = polite_wait

def main():
    print("=================================================================")
    print("=== StudioNet Smoke Tests: DeliveryProof Contract ===")
    print("=================================================================")
    print(f"Target Contract: {CONTRACT_ADDRESS}")
    
    deployer = get_deployer_account()
    buyer_client = create_client(chain=studionet, account=deployer)
    patch_client_for_rate_limits(buyer_client)
    
    print(f"Buyer / Timelock Keeper: {deployer.address}")
    buyer_bal = buyer_client.get_balance(deployer.address)
    print(f"Buyer Balance on StudioNet: {buyer_bal} wei ({buyer_bal / 10**18:.4f} GEN)")

    carrier = Account.create()
    carrier_client = create_client(chain=studionet, account=carrier)
    patch_client_for_rate_limits(carrier_client)
    print(f"Ephemeral Carrier Address: {carrier.address}")

    print("Funding Carrier account via StudioNet faucet...")
    buyer_client.fund_account(carrier.address, 3 * 10**18)
    time.sleep(4)
    carrier_bal = carrier_client.get_balance(carrier.address)
    print(f"Carrier Balance on StudioNet: {carrier_bal} wei ({carrier_bal / 10**18:.4f} GEN)")

    # 1. Smoke Test: deposit_funds (Payable write)
    print("\n--- 1. Executing deposit_funds (Buyer depositing 0.05 GEN) ---")
    deposit_wei = 50_000_000_000_000_000 # 0.05 GEN
    tx_hash_1 = buyer_client.write_contract(
        address=CONTRACT_ADDRESS,
        function_name="deposit_funds",
        args=[],
        value=deposit_wei,
    )
    print(f"Tx Hash: {tx_hash_1}")
    time.sleep(6)
    rec_1 = buyer_client.wait_for_transaction_receipt(
        tx_hash_1,
        wait_until="finalized",
        interval=5000,
        retries=30,
    )
    buyer_contract_bal = buyer_client.read_contract(
        address=CONTRACT_ADDRESS,
        function_name="get_balance",
        args=[deployer.address]
    )
    print(f"Deposit confirmed! Buyer internal contract balance: {buyer_contract_bal} wei")

    # 2. Smoke Test: create_shipment (Payable write)
    print("\n--- 2. Executing create_shipment (Buyer creating shipment with carrier) ---")
    freight_wei = 50_000_000_000_000_000  # 0.05 GEN
    bond_wei = 20_000_000_000_000_000     # 0.02 GEN
    manifest_id = f"MNF-STUDIONET-{int(time.time())}"
    dest_code = "JFK-COLD-HUB"

    tx_hash_2 = buyer_client.write_contract(
        address=CONTRACT_ADDRESS,
        function_name="create_shipment",
        args=[
            carrier.address,
            manifest_id,
            dest_code,
            freight_wei,
            bond_wei,
            72,   # max_transit_hours
            200,  # delay_penalty_bps_per_hour
            8,    # max_temp_celsius
            2500, # temp_breach_penalty_bps
            10,   # dispute_window_blocks
        ],
        value=0, # Deducts from buyer's internal contract balance
    )
    print(f"Tx Hash: {tx_hash_2}")
    time.sleep(6)
    rec_2 = buyer_client.wait_for_transaction_receipt(
        tx_hash_2,
        wait_until="finalized",
        interval=5000,
        retries=30,
    )
    
    state_after = buyer_client.read_contract(
        address=CONTRACT_ADDRESS,
        function_name="get_contract_state",
        args=[]
    )
    new_shipment_id = state_after["total_shipments"]
    print(f"Shipment created! New Total Shipments: {new_shipment_id}")

    s_data = buyer_client.read_contract(
        address=CONTRACT_ADDRESS,
        function_name="get_shipment",
        args=[new_shipment_id]
    )
    print(f"Live Shipment #{new_shipment_id} Data:")
    print(f"  Manifest: {s_data['manifest_id']}")
    print(f"  Buyer: {s_data['buyer']}")
    print(f"  Carrier: {s_data['carrier']}")
    print(f"  Status: {s_data['status']}")
    print(f"  Freight Escrow: {s_data['freight_payout']} wei")
    print(f"  Required Carrier Bond: {s_data['required_carrier_bond']} wei")
    assert s_data["status"] == "CREATED"

    # 3. Smoke Test: stake_carrier_bond (Payable write by Carrier)
    print(f"\n--- 3. Executing stake_carrier_bond (Carrier staking {bond_wei / 10**18:.2f} GEN performance bond) ---")
    tx_hash_3 = carrier_client.write_contract(
        address=CONTRACT_ADDRESS,
        function_name="stake_carrier_bond",
        args=[new_shipment_id],
        value=bond_wei,
    )
    print(f"Tx Hash: {tx_hash_3}")
    time.sleep(6)
    rec_3 = carrier_client.wait_for_transaction_receipt(
        tx_hash_3,
        wait_until="finalized",
        interval=5000,
        retries=30,
    )
    
    s_active = buyer_client.read_contract(
        address=CONTRACT_ADDRESS,
        function_name="get_shipment",
        args=[new_shipment_id]
    )
    print(f"\nLive Shipment #{new_shipment_id} State After Carrier Bond Stake:")
    print(f"  Status: {s_active['status']}")
    print(f"  Carrier Bond Locked: {s_active['carrier_bond_locked']} wei")
    final_state = buyer_client.read_contract(
        address=CONTRACT_ADDRESS,
        function_name="get_contract_state",
        args=[]
    )
    print(f"  Total Escrow Locked in Contract: {final_state['total_escrow_locked']} wei")
    assert s_active["status"] == "IN_TRANSIT"
    assert s_active["carrier_bond_locked"] == bond_wei

    print("\n=================================================================")
    print("[SUCCESS] All 3 Live Payable Smoke Tests Succeeded on StudioNet!")
    print(f"1. deposit_funds: Tx {tx_hash_1}")
    print(f"2. create_shipment: Tx {tx_hash_2} (Shipment #{new_shipment_id})")
    print(f"3. stake_carrier_bond: Tx {tx_hash_3} (Status transitioned to IN_TRANSIT)")
    print("=================================================================")

if __name__ == "__main__":
    main()
