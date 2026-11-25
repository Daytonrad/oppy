# Security Policy

[Português (Brasil)](pt-BR/SEGURANCA.md)

## Scope

Oppy is a personal, self-hosted opportunity monitor. The current development branch is `main`; there is no promised support period, patch schedule, security response SLA or bug bounty.

Relevant findings include credential exposure, unsafe handling of external HTML or URLs, unintended network requests, database or backup integrity failures, and disclosure of sensitive information through logs, fixtures or diagnostic snapshots. Source access restrictions must be respected.

## Reporting

Use the [Security Advisories page](https://github.com/kayo-willian/oppy/security/advisories) and **Report a vulnerability** if private vulnerability reporting is available. If it is not available, open a public issue requesting a private contact method, without including exploit details, tokens, private identifiers or raw evidence. Do not assume private reporting is enabled.

Include the affected commit, environment, expected boundary, observed behavior, impact, minimal safe reproduction and suggested fix if known. Distinguish reproduced findings from hypotheses. Use local fixtures and simulated endpoints; do not test against systems without permission.

Do not share real `.env` files, Telegram tokens, chat IDs, databases, backups, raw diagnostic HTML or unredacted logs. If a Telegram token is exposed, revoke it through BotFather and replace it locally.

Allow a reasonable opportunity to investigate before publishing exploitable details. Ordinary non-sensitive bugs belong in Issues. See [CONTRIBUTING.md](CONTRIBUTING.md) for safe testing.
