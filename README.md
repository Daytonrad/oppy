# OpportunityBeacon

I started OpportunityBeacon to discover opportunities without checking the same websites manually. The idea, requirements and direction are mine. I use OpenAI Codex to help implement the software.

OpportunityBeacon monitors public PSG selections for all course areas in Santarém, Pará. Senac Pará is the information source. The first valid collection produces a single summary. Subsequent observations can announce new courses, registration changes, revised dates and documents.

## Configure and run on Linux

Copy `.env.example` to `.env` in the repository root. Docker Compose reads this file. Set the following values as needed:

| Variable | Purpose |
|---|---|
| BEACON_DB_PATH | SQLite file, default `data/beacon.sqlite3`. In Compose keep it under `/app/data`, the persistent volume. |
| BEACON_INTERVAL_MINUTES | Minutes to wait after a completed cycle. Default 30, minimum 10. Confirm the permitted frequency with the source before starting recurring checks. |
| BEACON_FAILURE_ALERT_AFTER | Consecutive failed or inconclusive collections that trigger one incident alert. Default 3. Recovery queues one more message. |
| BEACON_TELEGRAM_TOKEN | Bot credential obtained through BotFather. Keep the real value in the local environment file. |
| BEACON_TELEGRAM_CHAT_ID | Destination chat for the bot. If either Telegram setting is empty, collection continues and messages stay pending. |
| BEACON_BACKUP_PATH | Optional new file for a backup. By default, a UTC-timestamped file is created beside the database. |

Start the scheduler, inspect its local state and stop it with:

```sh
docker compose up -d --build
docker compose exec beacon python -m opportunity_beacon status
docker compose logs --tail=50 beacon
docker compose down
```

The service uses outbound HTTPS, publishes no ports and restarts unless stopped. The volume survives container recreation and ordinary shutdown. The machine must be on and the service running to observe changes. A short registration window can be missed while offline. After changing credentials, recreate the service with `docker compose up -d`.

For a native Linux run, export the configuration in your shell and use `python -m opportunity_beacon run` with Python 3.11 or newer and system timezone data. The no-argument command initializes the SQLite file and exits. Status, backup and the manual checks below are available through the same module.

## Manual checks

Run these individually when ready to access the real services. To test before starting recurring monitoring, build the image and use one-off containers:

```sh
docker compose build
docker compose run --rm beacon python -m opportunity_beacon smoke
docker compose run --rm beacon python -m opportunity_beacon smoke-telegram
```

The first command after the build checks one listing and the linked Santarém details, prints a summary and exits. It skips retries, SQLite and notifications. The Telegram check sends one test message to the configured chat. The automated test suite uses fixtures and loopback simulations.

## Back up and restore

Use SQLite's online backup while the service runs:

```sh
docker compose exec beacon python -m opportunity_beacon backup
```

The command prints the destination. An explicit new path after `backup` takes precedence over the environment setting. Existing backups are preserved. Keep container backups under `/app/data` and copy the completed snapshot out of the container for independent storage. To restore, stop the scheduler, place the snapshot at a fresh path in the volume, point `BEACON_DB_PATH` to that path and restart. Pending messages in the restored snapshot are eligible for delivery.

## Reliability

The scheduler runs one cycle at a time and waits after completion. Its database lock prevents another scheduler on the same local database. Connection and total deadlines are 5 and 15 seconds per attempt, with a 2 MiB HTML limit. The client declines redirects. Transient connection errors and HTTP 500, 502 or 504 get at most one retry after one second. HTTP 403, 429 and 503 end the cycle immediately. An unsuccessful cycle doubles the delay progressively, capped at six hours (or the configured base interval if longer). A longer Retry-After takes precedence and survives restart. A successful cycle restores the normal interval. SIGTERM interrupts scheduled waits and lets an in-flight bounded request finish.

Delivery is **at-least-once**. A crash after Telegram accepts a message but before SQLite records success may produce a duplicate. A Telegram outage pauses that delivery batch. The next collection still runs. See the storage and delivery details in [the core documentation](docs/offline-core.md).

## Run the offline tests

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install "pip>=25.1"
python -m pip install --group dev
python -m pytest -q
```

CI runs only the offline tests. The runtime uses the Python standard library.

MIT license.
