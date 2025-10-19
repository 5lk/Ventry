import base64
from typing import Dict, Any
from algosdk import account, mnemonic
from algosdk.v2client.algod import AlgodClient
from algosdk.transaction import (
    PaymentTxn, AssetConfigTxn, AssetTransferTxn,
    ApplicationCreateTxn, ApplicationNoOpTxn,
    StateSchema, OnComplete, wait_for_confirmation, assign_group_id
)

ALGOD_ADDRESS = "http://localhost:4001"
ALGOD_TOKEN   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
algod_client  = AlgodClient(ALGOD_TOKEN, ALGOD_ADDRESS)

FUNDED_MNEMONIC = "myth unveil brain keep abandon odor daring ripple another prefer source suggest drop rigid gospel foot vague certain hire peace impulse evil trap ability raven"
FUNDED_SK  = mnemonic.to_private_key(FUNDED_MNEMONIC)
FUNDED_ADDR = account.address_from_private_key(FUNDED_SK)

SCALE_GBP = 100  # pence

APPROVAL_SOURCE = """
#pragma version 8
txn ApplicationID
int 0
==
bnz init

txna ApplicationArgs 0
byte "update_token_price"
==
bnz update_price

err

init:
byte "company_addr"
txn Sender
app_global_put

byte "token_id"
txna ApplicationArgs 1
btoi
app_global_put

byte "token_price"
txna ApplicationArgs 2
btoi
app_global_put
int 1
return

update_price:
byte "company_addr"
app_global_get
txn Sender
==
bz unauthorized

byte "token_price"
txna ApplicationArgs 1
btoi
app_global_put
int 1
return

unauthorized:
int 0
return
"""

CLEAR_SOURCE = """
#pragma version 8
int 1
return
"""

def suggested():
    return algod_client.suggested_params()

def compile_source(source: str) -> bytes:
    res = algod_client.compile(source)
    return base64.b64decode(res["result"])

def generate_wallet():
    sk, addr = account.generate_account()
    return sk, addr, mnemonic.from_private_key(sk)

def fund_account(receiver_addr: str, amount_microalgos: int):
    txn = PaymentTxn(FUNDED_ADDR, suggested(), receiver_addr, amount_microalgos)
    stxn = txn.sign(FUNDED_SK)
    txid = algod_client.send_transaction(stxn)
    wait_for_confirmation(algod_client, txid, 4)
    return txid

def create_asa(creator_sk: bytes, unit_name: str, asset_name: str, total_supply: int, decimals: int = 0) -> int:
    sender = account.address_from_private_key(creator_sk)
    txn = AssetConfigTxn(
        sender=sender, sp=suggested(),
        total=total_supply, decimals=decimals, default_frozen=False,
        unit_name=unit_name, asset_name=asset_name,
        manager=sender, reserve=sender, freeze=sender, clawback=sender,
        url="http://example.com"
    )
    stxn = txn.sign(creator_sk)
    txid = algod_client.send_transaction(stxn)
    res = wait_for_confirmation(algod_client, txid, 4)
    return res["asset-index"]

def ensure_opt_in(acct_sk: bytes, asset_id: int):
    addr = account.address_from_private_key(acct_sk)
    txn = AssetTransferTxn(sender=addr, sp=suggested(), receiver=addr, amt=0, index=asset_id)
    stxn = txn.sign(acct_sk)
    txid = algod_client.send_transaction(stxn)
    wait_for_confirmation(algod_client, txid, 4)
    return txid

def transfer_asa(sender_sk: bytes, receiver_addr: str, asset_id: int, amount: int):
    sender_addr = account.address_from_private_key(sender_sk)
    txn = AssetTransferTxn(sender=sender_addr, sp=suggested(), receiver=receiver_addr, amt=amount, index=asset_id)
    stxn = txn.sign(sender_sk)
    txid = algod_client.send_transaction(stxn)
    wait_for_confirmation(algod_client, txid, 4)
    return txid

def deploy_price_app(company_sk: bytes, token_id: int, initial_price_scaled_gbp: int) -> int:
    creator_addr = account.address_from_private_key(company_sk)
    approval = compile_source(APPROVAL_SOURCE)
    clear    = compile_source(CLEAR_SOURCE)
    txn = ApplicationCreateTxn(
        sender=creator_addr, sp=suggested(),
        on_complete=OnComplete.NoOpOC,
        approval_program=approval, clear_program=clear,
        global_schema=StateSchema(num_uints=2, num_byte_slices=1),
        local_schema=StateSchema(num_uints=0, num_byte_slices=0),
        app_args=[b"init", token_id.to_bytes(8,"big"), initial_price_scaled_gbp.to_bytes(8,"big")]
    )
    stxn = txn.sign(company_sk)
    txid = algod_client.send_transaction(stxn)
    res = wait_for_confirmation(algod_client, txid, 4)
    return res["application-index"]

def update_token_price(company_sk: bytes, app_id: int, new_price_scaled_gbp: int):
    sender = account.address_from_private_key(company_sk)
    app_args = [b"update_token_price", new_price_scaled_gbp.to_bytes(8, "big")]
    txn = ApplicationNoOpTxn(sender, suggested(), app_id, app_args)
    stxn = txn.sign(company_sk)
    txid = algod_client.send_transaction(stxn)
    wait_for_confirmation(algod_client, txid, 4)
    return txid

def get_app_state(app_id: int) -> Dict[str, Any]:
    info = algod_client.application_info(app_id)
    gs = info["params"].get("global-state", [])
    out = {}
    for kv in gs:
        k = base64.b64decode(kv["key"]).decode("utf-8")
        v = kv["value"]
        if v["type"] == 2: out[k] = v["uint"]
        elif v["type"] == 1: out[k] = base64.b64decode(v["bytes"])
    return out

def atomic_approve_and_pay(company_sk: bytes, company_addr: str, dev_addr: str, app_id: int,
                           asset_id: int, upfront_microalgos: int, token_amount: int,
                           new_price_scaled_gbp: int):
    sp = suggested()
    pay_txn = PaymentTxn(company_addr, sp, dev_addr, upfront_microalgos)
    asa_txn = AssetTransferTxn(company_addr, sp, dev_addr, token_amount, asset_id)
    app_args = [b"update_token_price", new_price_scaled_gbp.to_bytes(8,"big")]
    app_txn = ApplicationNoOpTxn(company_addr, sp, app_id, app_args)

    assign_group_id([pay_txn, asa_txn, app_txn])
    stxns = [pay_txn.sign(company_sk), asa_txn.sign(company_sk), app_txn.sign(company_sk)]
    txid = algod_client.send_transactions(stxns)
    wait_for_confirmation(algod_client, txid, 4)
    return txid
