import subprocess
from eth_account import Account
from genlayer_py import create_client, studionet

CONTRACT_ADDRESS = "0x2FcD9bbbad3d37b39BC440d9d0cAe34Ac9f9d6f8"

def get_deployer_account():
    cmd = [
        "node", "-e",
        "const keytar = require('C:/Users/NO GO NO/AppData/Roaming/npm/node_modules/genlayer/node_modules/keytar'); "
        "keytar.getPassword('genlayer-cli', 'account:ace-deployer').then(p => process.stdout.write(p));"
    ]
    pk = subprocess.check_output(cmd, text=True).strip()
    return Account.from_key(pk)

account = get_deployer_account()
client = create_client(chain=studionet, account=account)

state = client.read_contract(
    address=CONTRACT_ADDRESS,
    function_name="get_contract_state",
    args=[]
)
print("Contract State:", state)

bal = client.read_contract(
    address=CONTRACT_ADDRESS,
    function_name="get_balance",
    args=[account.address]
)
print(f"Deployer Internal Contract Balance: {bal} wei")
