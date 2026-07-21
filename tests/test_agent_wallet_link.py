"""EIP-712 parity with the booa.app Bridge (src/lib/contracts/agent-wallet.ts).

The digest MUST match viem's hashTypedData for the same input, or the identity
registry rejects the OWS signature. The reference below was produced by viem.
"""
import base64
import json

from booa.agent_wallet_link import (
    build_typed_data,
    typed_data_digest,
    encode_blob,
)

# Shared test vector (identical to src/__tests__/agent-wallet-signature.test.ts).
CHAIN_ID = 1
AGENT_ID = 36637
NEW_WALLET = "0x9c54a9c609212d2fd034b55cf3b42ba99af52880"
OWNER = "0xde152AfB7db5373F34876E1499fbD893A82dD336"  # adapter
DEADLINE = 1800000000

# Produced by viem hashTypedData(buildAgentWalletTypedData(...)) for the vector above.
VIEM_DIGEST = "0x95548c24a4e4188ce9758f73febb2b223e7f25d05366b7635ff0694df8468faa"


def test_digest_matches_viem_reference():
    typed = build_typed_data(CHAIN_ID, AGENT_ID, NEW_WALLET, OWNER, DEADLINE)
    assert typed_data_digest(typed) == VIEM_DIGEST


def test_digest_is_checksum_independent():
    lower = build_typed_data(CHAIN_ID, AGENT_ID, NEW_WALLET.lower(), OWNER.lower(), DEADLINE)
    assert typed_data_digest(lower) == VIEM_DIGEST


def test_digest_binds_agent_owner_deadline():
    base = typed_data_digest(build_typed_data(CHAIN_ID, AGENT_ID, NEW_WALLET, OWNER, DEADLINE))
    assert typed_data_digest(build_typed_data(CHAIN_ID, AGENT_ID + 1, NEW_WALLET, OWNER, DEADLINE)) != base
    assert typed_data_digest(build_typed_data(CHAIN_ID, AGENT_ID, NEW_WALLET, OWNER, DEADLINE + 1)) != base
    other_owner = "0x2222222222222222222222222222222222222222"
    assert typed_data_digest(build_typed_data(CHAIN_ID, AGENT_ID, NEW_WALLET, other_owner, DEADLINE)) != base


def test_blob_shape_matches_bridge_decoder():
    sig = "0x" + "ab" * 65
    blob = encode_blob(CHAIN_ID, AGENT_ID, NEW_WALLET, DEADLINE, sig)
    decoded = json.loads(base64.b64decode(blob).decode())
    assert decoded == {
        "v": 1,
        "chainId": 1,
        "agentId": "36637",
        "wallet": NEW_WALLET,
        "deadline": "1800000000",
        "signature": sig,
    }
