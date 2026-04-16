---
name: ows
version: 1.0.0
description: Wallet setup and management via Open Wallet Standard. Create a new OWS wallet, import an existing vault, validate on-chain status, and link the wallet to the BOOA agent on ERC-8004. Uses the local OWS vault with policy-gated signing.
homepage: https://openwallet.sh
metadata: {"emoji":"🔐","vault_path":"/data/.hermes/wallets/","dashboard_sync":"/data/.agent/wallet-info.txt","primary_chains":["shape","base"]}
---

# /ows — Wallet Setup

Set up, import, and link wallets for your BOOA agent using Open Wallet Standard (OWS).

## Triggers

Invoke this skill when the operator says any of:

- `/ows` — direct slash command
- "set up my wallet", "set up a wallet", "create wallet"
- "import my wallet", "import OWS vault", "use my existing wallet"
- "link wallet to 8004", "register agent wallet"

## Reference Docs

| Reference | Description |
|-----------|-------------|
| [wallet-setup.md](references/wallet-setup.md) | Full OWS flow, existing wallet import, ERC-8004 ownership scenarios (A/B/C) |

---

## Flow

### Step 1 — Identify the scenario

Ask the operator which flow applies:

1. **New wallet** — create a fresh OWS vault for the agent (recommended default)
2. **Import existing** — operator supplies an existing OWS vault (mnemonic or exported vault file)
3. **Link only** — wallet already set up, just needs to be linked to the 8004 identity

### Step 2 — Execute

Follow `references/wallet-setup.md` step-by-step for the chosen scenario.

Hermes-specific path overrides (use these, not the defaults in wallet-setup.md):

| Purpose | Path |
|---|---|
| OWS vault | `/data/.hermes/wallets/` |
| Policies | `/data/.hermes/wallets/policies/` |
| Dashboard sync file | `/data/.agent/wallet-info.txt` |

Set `OWS_VAULT_DIR=/data/.hermes/wallets` before running OWS commands so vault files land in the persistent Hermes volume.

### Step 3 — On-chain validation (REQUIRED after any wallet operation)

Before considering the setup done, validate on-chain state. Use the token ID from `context/agent.json`.

```bash
# Fetch current NFT owner and 8004 registration
curl -s "https://khora.fun/api/agent-registry/360/<TOKEN_ID>"
```

Check three things:

1. **NFT ownership** — `ownerOf(tokenId)` on the BOOA contract. Compare against the new wallet address.
2. **8004 registration** — `agents[agentId].wallet` on the identity registry. Compare against the new wallet.
3. **Orphan detection** — if `nftOwner != agentWallet`, the agent is orphaned. Report this to the operator and ask how to proceed (Scenario A, B, or C in wallet-setup.md).

### Step 4 — Dashboard sync (REQUIRED)

The dashboard at `http://<your-railway-url>` reads the agent's wallet address from a single file:

```
/data/.agent/wallet-info.txt
```

Format (exactly this — the dashboard parses line-by-line):

```
EVM Address: 0xYourAgentWalletAddress
```

After any successful wallet setup or import, write/overwrite this file. Without it, the dashboard will keep showing `not set — tell your agent "set up my wallet"`.

```bash
mkdir -p /data/.agent
echo "EVM Address: 0xYourAgentWalletAddress" > /data/.agent/wallet-info.txt
chmod 644 /data/.agent/wallet-info.txt
```

### Step 5 — Report to operator

Summarise in chat (never reveal the private key or mnemonic):

- Wallet address (EVM)
- Chains funded and balances
- 8004 link status (linked / not linked / orphan)
- Which scenario was executed (A / B / C) and what the operator still needs to do
- Confirmation that `wallet-info.txt` was updated — tell the operator to refresh the dashboard

---

## Security rules (non-negotiable)

- Never display mnemonic phrases, seed phrases, or private keys in chat.
- Never `cat` or echo `.env`, `config.yaml`, `wallet-info.txt`, or any file under `/data/.hermes/wallets/` in chat.
- Use the OWS CLI for all key operations. Never use Python wallet SDKs to export or generate keys.
- Every signed transaction requires explicit operator approval — summarise what is being signed before asking for confirmation.
- If OWS CLI is not installed, tell the operator to install it. Do not fall back to SDK-based key handling.
