# Contributing to BOOA Hermes Template

Thanks for your interest in contributing.

## Getting Started

```bash
git clone https://github.com/0xmonas/booa-hermes-template.git
cd booa-hermes-template
docker build -t booa-hermes .
docker run -d -p 8080:8080 -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=test -v booa-data:/data booa-hermes
```

Open http://localhost:8080 to test.

## Project Structure

```
server.py              — Admin server (Starlette + Uvicorn)
booa/
  fetcher.py           — Fetch BOOA identity from khora.fun API
  writer.py            — Write Hermes files (SOUL.md, USER.md, skills, config)
  gateway.py           — Hermes gateway subprocess manager
  onboarding.py        — Wizard state management
templates/             — Jinja2 HTML templates
static/                — CSS + JS
```

## Guidelines

- Keep the setup wizard simple. Non-dev holders should be able to complete it.
- Don't store sensitive data (mnemonic, private keys) in logs, chat, or unencrypted files.
- Test with a real BOOA token ID (e.g., 1496) before submitting.
- Follow the existing code style — no frameworks beyond Starlette.

## Security

If you find a security issue, please DM [@0xmonas](https://twitter.com/0xmonas) instead of opening a public issue.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
