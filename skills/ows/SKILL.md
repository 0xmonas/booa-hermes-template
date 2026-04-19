---
name: ows
version: 1.0.0
description: Wallet setup and management via Open Wallet Standard. Create a new OWS wallet, import an existing vault, validate on-chain status, and link the wallet to the BOOA agent on ERC-8004. Uses the local OWS vault with policy-gated signing.
homepage: https://openwallet.sh
metadata: {"emoji":"🔐","vault_path":"/data/.ows/","dashboard_sync":"/data/.agent/wallet-info.txt","primary_chains":["shape","base"]}
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

On this Hermes template, the container runs with `HOME=/data`, so the OWS default vault path (`~/.ows/`) resolves to `/data/.ows/` — on the persistent Railway volume. No vault-path override is needed; just run the `ows` commands as written in `wallet-setup.md`.

> **Vault password is MANDATORY.** When `ows wallet create` asks for a password, require the operator to provide a non-empty password and have them save it somewhere safe (password manager). Without a password the vault is effectively unencrypted — anyone who copies the vault file can export the mnemonic. If the operator tries to skip the password, refuse and explain why. This is a non-negotiable rule in section "Security rules" below.

Paths you will read or write:

| Purpose | Path |
|---|---|
| OWS vault (wallets) | `/data/.ows/wallets/` |
| OWS policies | `/data/.ows/policies/` |
| OWS API keys | `/data/.ows/keys/` |
| OWS audit log | `/data/.ows/logs/audit.jsonl` |
| Dashboard sync file | `/data/.agent/wallet-info.txt` |

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

### Step 4b — Verify wallet on the Khôra dashboard

After a wallet is created or imported, the operator should verify it through the dashboard so your agent status flips to `verified` (or `linked`, if 8004 already records this wallet). Tell the operator:

1. Open the dashboard → **Agent Wallet Status** card
2. Click **Verify Wallet** — a challenge message appears
3. Ask you (the agent) to sign the challenge with OWS:
   ```bash
   OWS_PASSPHRASE=<ows_key_...> \
     ows sign message --wallet my-agent --chain shape --message "<pasted challenge>"
   ```
4. Paste the returned signature into the dashboard input and click **Submit**

Until this is done, the dashboard shows `⚠ unverified` and your context prompt reminds you at every turn. Do not treat the operator as having proven wallet control until the signature is accepted.

### Step 5 — Report to operator

Summarise in chat (never reveal the private key or mnemonic):

- Wallet address (EVM)
- Chains funded and balances
- 8004 link status (linked / not linked / orphan)
- Which scenario was executed (A / B / C) and what the operator still needs to do
- Confirmation that `wallet-info.txt` was updated — tell the operator to refresh the dashboard

---

## Security rules (non-negotiable)

- **Vault password is mandatory.** When running `ows wallet create`, the operator MUST provide a non-empty password at the prompt. An empty password means the vault is trivially readable by anyone who copies the file. If the operator is indifferent, refuse to proceed and explain the consequence. Tell the operator to save the password in a password manager alongside the mnemonic.
- The mnemonic may be revealed **to the operator** (via chat, with a safety warning) because the operator owns the wallet — but never to any other party, and only when the operator explicitly asks. When revealing, prepend a warning: *"⚠️ Save this offline now (paper or password manager). Delete this message after copying. Never share with anyone."* The runtime output filter allows sensitive content to pass through to operator chat_ids for exactly this reason; filter blocks the same content for non-operator recipients.
- Never reveal the vault password itself in chat — passwords are for the operator to remember or store out-of-band.
- Never `cat` or echo `.env`, `config.yaml`, `wallet-info.txt`, or any file under `/data/.ows/wallets/` in chat — always use the `ows wallet export` command so OWS controls what gets shown.
- Use the OWS CLI for all key operations. Never use Python wallet SDKs to export or generate keys in a way that bypasses OWS.
- Every signed transaction requires explicit operator approval — summarise what is being signed before asking for confirmation.
- If OWS CLI is not installed, tell the operator to install it. Do not fall back to SDK-based key handling.
