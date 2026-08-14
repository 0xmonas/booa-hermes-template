# BOOA Hermes Template

Deploy your BOOA as an autonomous AI agent. One-click deploy on Railway, zero terminal interaction.

By [BOOA](https://booa.app)

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/booa-hermes-template?referralCode=gD4PGO&utm_medium=integration&utm_source=template&utm_campaign=generic)

[View on Railway Marketplace](https://railway.com/deploy/booa-hermes-template)

---

## What This Does

You enter your BOOA token ID. The template fetches your agent's on-chain identity — personality, skills, boundaries, pixel art — and sets up [Hermes Agent](https://github.com/NousResearch/hermes-agent) with everything pre-loaded.

Your agent comes with:
- **SOUL.md** — your BOOA's on-chain personality
- **IDENTITY.md** — creature type, vibe, appearance
- **BOOA skill** — agent setup, wallet config, ERC-8004 ownership
- **Cobbee skill** — creator platform, x402 payments

## Setup

### 1. Attach a volume (one-time, ~10 seconds)

After clicking Deploy, Railway creates the service but **does not** attach persistent storage by default — this is intentional so that template updates don't reset your data. You need to add a volume yourself, once:

1. Open your new service in Railway → **Settings** → **Volumes**
2. Click **Add Volume**
3. Mount path: `/data`
4. Save — the service will restart automatically

Without this, your agent's memory, pairing, and wallet live only in the container's ephemeral filesystem and will disappear on redeploys.

### 2. Run the wizard (4 steps)

Open your app URL. After login (`admin` / your `ADMIN_PASSWORD`) the wizard opens:

1. **Token ID** — Enter your BOOA token ID. Identity fetched from the blockchain.
2. **USER.md** — Tell your agent about yourself. What to do, spending limits, language.
3. **OpenRouter API key** — Sign up for free at [openrouter.ai](https://openrouter.ai) and paste your key. OpenRouter is the one supported provider in the wizard because the same key covers your agent's main model and Hermes's auxiliary tasks (summarization, compression) — no "No auxiliary LLM configured" warnings. Pick from free tiers (e.g. `openai/gpt-oss-120b:free`) or paid models (Claude, GPT-5, Gemini, DeepSeek, Llama, Mistral). Power users can still override to Anthropic / custom endpoints via Railway Variables after setup.
4. **Telegram** — Create a bot via @BotFather, paste the token. Done.

Your agent starts automatically. Message it on Telegram.

### Updating the template

When Railway shows "Check for updates", accepting will rebuild the service but **leave your volume untouched** — SOUL.md, memory, sessions, OWS vault, Telegram pairing all survive. The startup script re-creates the expected directory structure idempotently, so no manual migration is needed.

## After Setup

Your agent can:
- Research, write code, browse the web, manage files
- Set up its own wallet — tell it: "set up my wallet"
- Join Cobbee as a creator — tell it: "/cobbee"
- Install skills from the community
- Learn from experience and create its own skills
- Remember conversations across sessions

## Dashboard

- BOOA pixel art and identity display
- ERC-8004 verification status
- Wallet address with 8004 linking status
- Gateway controls (start / stop)
- Telegram pairing (approve / deny users)
- Live gateway logs
- Settings (provider, model, channels)
- Data export (ZIP with all agent files)

## Requirements

- A BOOA NFT — [opensea.io/collection/booa](https://opensea.io/collection/booa)
- An AI provider API key — [OpenRouter](https://openrouter.ai/) (free tier available)
- A Telegram bot token — [@BotFather](https://t.me/BotFather)
- Railway account — ~$5/month

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ADMIN_USERNAME` | Yes | Dashboard login username |
| `ADMIN_PASSWORD` | Yes | Dashboard login password |
| `OPENROUTER_API_KEY` | No | Model API key. The wizard asks for it once; set it here to replace or rotate it later — it syncs into the agent's config on every restart |
| `TELEGRAM_BOT_TOKEN` | No | Same: set here to rotate the bot token after setup |
| `OPENSEA_API_KEY` | No | Enables OpenSea search + trading tools (free key: docs.opensea.io/reference/api-keys) |
| `BOOA_ONCHAIN_MCP`, `BOOA_ONCHAIN_WRITES`, `BOOA_OPENSEA_MCP` | No | Enable onchain read tools / trading + wallet actions / OpenSea. Also settable from the dashboard's Onchain & Trading card |
| `BOOA_MAX_TX_ETH`, `BOOA_DAILY_CAP_ETH`, `BOOA_SEND_ALLOWLIST`, `BOOA_SWAP_TOKEN_ALLOWLIST`, `BOOA_MAX_SLIPPAGE_BPS`, `BOOA_OPENSEA_REQUIRE_VERIFIED` | No | Trading guardrails — per-tx and daily ETH caps, destination and token allowlists, slippage cap, verified-only buying. Dashboard values win over env |
| `OWS_PASSPHRASE` | No | Scoped OWS API key for autonomous signing (never the vault password) |
| `ETH_RPC`, `BASE_RPC` | No | Custom RPC endpoints |

API keys and secrets are set here (Railway → Variables), never in the dashboard. Everything else is configured through the web dashboard.

## Security

**Your dashboard is on the public internet.** Railway gives the service a public URL, and the
hostname follows a predictable pattern, so assume strangers can find it. `ADMIN_PASSWORD` is the
only thing standing between them and an agent that holds a wallet — so make it long and random
(a password manager string, 24+ characters), never reuse it, and change it if you ever paste it
somewhere. If you only use Telegram, you can remove the public domain in Railway → Settings →
Networking after setup and the agent keeps working.

What the template does for you:

- Admin auth cannot be turned off; there is no default or shared password — yours is unique to your instance
- A correct password is never rate-limited, so nobody can lock you out by spamming wrong guesses; wrong guesses are throttled instead
- Changing `ADMIN_PASSWORD` immediately invalidates every existing dashboard session
- Session cookies are `Secure`, `HttpOnly`, `SameSite=Lax`, and expire in 24 hours
- Dashboard credentials are stripped from the agent's environment, so a prompt injection cannot read them
- Admin password re-confirmation required for both export and import
- Backups ship the encrypted wallet vault only — never the signing key, vault passphrase, or API keys
- Mnemonics never displayed in chat (SECURITY.md rules) and filtered out of exports
- OWS wallet vault encrypted at rest (AES-256-GCM)
- Web console is off by default, uses a separate rotatable key, and can never reach the dashboard
- Logs streamed to the browser are scanned and redacted for keys and secrets first
- Gateway runs as an isolated subprocess, authenticated with its own per-install key
- ERC-8004 verification checked on every dashboard load

## Data Persistence

All data lives on a Railway persistent volume at `/data`. Survives container restarts and redeploys.

```
/data/hermes/
├── SOUL.md               — Agent personality (from blockchain)
├── config.yaml           — Provider, model, gateway config
├── memories/             — MEMORY.md + USER.md
├── skills/               — BOOA, Cobbee, and agent-created skills
├── sessions/             — Conversation history (SQLite + JSON)
├── context/              — IDENTITY.md, avatar.svg, SECURITY.md
└── platforms/pairing/    — Approved Telegram users
/data/.ows/
└── wallets/              — Encrypted OWS wallet vault
```

## Powered By

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT)
- [BOOA](https://booa.app) — on-chain AI agent identity
- [ERC-8004](https://8004.org) — trustless agent standard
- [BOOA](https://opensea.io/collection/booa) — 3,333 on-chain AI agents on Ethereum

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This is a self-hosted, non-custodial utility template. If you enable them, the agent can send, swap, sign, trade NFTs, and make payments using **your own wallet and funds, entirely at your own risk**. Blockchain transactions are irreversible, and the authors take no custody and give no warranty. Read [DISCLAIMER.md](DISCLAIMER.md) before enabling any wallet or trading feature.

## License

[MIT](LICENSE)
