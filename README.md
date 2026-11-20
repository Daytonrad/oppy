![OPPY pixel-art sign](assets/oppy-cover.svg)

# Oppy

I use Oppy to track free PSG courses in Santarém and receive changes through Telegram.

[![MIT license](https://img.shields.io/badge/license-MIT-blue?style=flat)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat)
![Docker Compose](https://img.shields.io/badge/Docker-Compose-2468a2?style=flat)
![SQLite](https://img.shields.io/badge/storage-SQLite-486477?style=flat)
[![Telegram bot](https://img.shields.io/badge/Telegram-bot-2987ac?style=flat)](https://t.me/oppybeacon_bot)
[![Tests](https://img.shields.io/github/actions/workflow/status/kayo-willian/oppy/tests.yml?branch=main&label=tests&style=flat)](https://github.com/kayo-willian/oppy/actions/workflows/tests.yml)
![Built with vibe coding](https://img.shields.io/badge/built%20with-vibe%20coding-c87824?style=flat)

## What it does

Oppy watches Programa Senac de Gratuidade selections in Santarém across all course areas. I get alerts for new selections and courses, future registration windows, openings, closures, filled places, reopenings, revised dates, new documents and amendments. After consecutive unsuccessful checks, Oppy alerts me to the failure and later to recovery. It only observes public offers and sends notifications. It never submits applications or collects candidates’ personal data.

## How it works

```text
PSG portal → parser → SQLite → outbox → Telegram
```

- The first valid collection queues one summary. Long summaries travel in numbered parts with whole course blocks.
- Within a selection, I match courses by a unique source ID first, then by normalized title and venue. The course period breaks ties between otherwise identical classes.
- A failed or inconclusive collection preserves the last valid state. A missing municipality panel cannot erase the course list.
- Each change and its pending notification commit together. The outbox uses at-least-once delivery and records success after Telegram confirms receipt.
- HTTP 403, 429 or 503 ends the current cycle. Later cycles back off and honor a longer Retry-After. A successful collection restores the configured interval.

See the [parser rules](docs/psg-parser.md) and [SQLite and delivery notes](docs/offline-core.md).

## Quick start

On Linux with Docker Compose, I run these commands from the repository root. I configure my local credentials before testing Telegram.

| Task | Command |
|---|---|
| Prepare local configuration | `cp .env.example .env`, then edit `.env` |
| Build the image | `docker compose build` |
| Check the portal once | `docker compose run --rm oppy python -m oppy smoke` |
| Send one test message | `docker compose run --rm oppy python -m oppy smoke-telegram` |
| Start the scheduler | `docker compose up -d` |
| Inspect the last checks and pending messages | `docker compose exec oppy python -m oppy status` |
| Print the saved summary | `docker compose exec oppy python -m oppy summary` |
| Send the saved summary directly | `docker compose exec oppy python -m oppy summary --send` |
| Create a consistent SQLite backup | `docker compose exec oppy python -m oppy backup` |

For an existing installation, stop the previous Compose service before starting Oppy. Keep its volume when shutting down. Compose explicitly reuses `opportunity-beacon_beacon-data` across directory and project renames. Both the database and its WAL files stay under `/app/data`. Avoid `docker compose down -v`, which removes volumes.

## Configuration

I kept the `BEACON_` prefix for compatibility with existing `.env` files.

| Variable | Meaning and default |
|---|---|
| `BEACON_DB_PATH` | SQLite path. Defaults to `data/beacon.sqlite3`. Keep it under the mounted data directory in Compose. |
| `BEACON_INTERVAL_MINUTES` | Minutes to wait after a completed cycle. Default 30, minimum 10. |
| `BEACON_FAILURE_ALERT_AFTER` | Consecutive failed or inconclusive checks before one incident alert. Default 3. |
| `BEACON_TELEGRAM_TOKEN` | Bot token from BotFather. Empty by default. |
| `BEACON_TELEGRAM_CHAT_ID` | Destination chat. If either Telegram setting is empty, notifications remain pending. |
| `BEACON_BACKUP_PATH` | New destination file. Defaults to a timestamped file beside the database. |

## Limitations

I only receive observations while the machine and service are running. A short registration window can pass between checks. At-least-once delivery can produce a duplicate if the process stops after Telegram accepts a message and before SQLite records success.

In-person registration can have an unknown status. Extraction depends on the portal’s HTML. A complete cycle requests the listing once and follows one detail link per Santarém selection, with at most one retry for eligible transient errors.

## Tests

I use saved HTML, temporary SQLite databases and local HTTP simulations. CI runs the offline suite. Checks against the live portal are manual.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install "pip>=25.1"
python -m pip install --group dev
python -m pytest -q
```

## Independence note

Oppy is my independent project. It has no official institutional affiliation, partnership or sponsorship. Senac Pará is the source of the public course information. I received written confirmation of access from the Senac administration.

## How this project was built

I came up with Oppy and defined its goals, requirements, priorities and limits. I did not write the code by hand: the implementation was done through vibe coding, with AI tools working in separate roles.

| Role | Who |
|---|---|
| Idea, requirements, decisions, testing on my own machine, reviewing and merging every pull request | Me |
| Initial project brief | ChatGPT 6 (high effort) |
| Prompt writing for each phase, review of Codex's reports, technical review of the plan | Claude Sonnet 5.5 (medium effort) |
| Code, tests and Docker setup | OpenAI Codex, GPT-6.2 Model Astra (medium effort) |

ChatGPT drafted the first brief, Claude reviewed it and suggested improvements (an outbox for notifications, hashes of normalized fields, offline tests with saved HTML, and a clear difference between a failed collection and an empty one), and I sent each phase to Codex as a prompt. Codex opened one pull request per phase. I reviewed each one, ran the Docker and smoke tests myself, and merged it only when I was satisfied. Claude did not have access to the repository and did not run the code; it worked from the reports and outputs I shared.

## License

[MIT](LICENSE).
