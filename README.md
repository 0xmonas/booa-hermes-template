# BOOA Hermes Template

Deploy your BOOA as an autonomous AI agent. One-click deploy on Railway, zero terminal interaction.

By [Khôra](https://khora.fun)

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/dY7R1A?referralCode=gD4PGO&utm_medium=integration&utm_source=template&utm_campaign=generic)

[View on Railway Marketplace](https://railway.com/deploy/booa-hermes-template)

---

## What This Does

You enter your BOOA token ID. The template fetches your agent's on-chain identity — personality, skills, boundaries, pixel art — and sets up [Hermes Agent](https://github.com/NousResearch/hermes-agent) with everything pre-loaded.

Your agent comes with:
- **SOUL.md** — your BOOA's on-chain personality
- **IDENTITY.md** — creature type, vibe, appearance
- **Khôra skill** — agent setup, wallet config, ERC-8004 ownership
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
3. **Provider** — Pick an AI provider. OpenRouter has a free tier.
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

Everything else is configured through the web dashboard.

## Security

- Session-based admin auth (signed cookies, persistent across restarts)
- Password re-confirmation required for data export
- Mnemonic phrases never displayed in chat (enforced via SECURITY.md context rules)
- Mnemonic automatically filtered from export files
- OWS wallet vault encrypted at rest (AES-256-GCM)
- Gateway runs as isolated subprocess
- ERC-8004 verification checked on every dashboard load

## Data Persistence

All data lives on a Railway persistent volume at `/data`. Survives container restarts and redeploys.

```
/data/hermes/
├── SOUL.md               — Agent personality (from blockchain)
├── config.yaml           — Provider, model, gateway config
├── memories/             — MEMORY.md + USER.md
├── skills/               — Khôra, Cobbee, and agent-created skills
├── sessions/             — Conversation history (SQLite + JSON)
├── context/              — IDENTITY.md, avatar.svg, SECURITY.md
└── platforms/pairing/    — Approved Telegram users
/data/.ows/
└── wallets/              — Encrypted OWS wallet vault
```

## Powered By

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research (MIT)
- [Khôra](https://khora.fun) — on-chain AI agent identity
- [ERC-8004](https://8004.org) — trustless agent standard
- [BOOA](https://opensea.io/collection/booa) — 3,333 on-chain AI agents on Shape Network

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
