# OpportunityBeacon

I started OpportunityBeacon to follow opportunities without checking the same websites manually. The idea, requirements and project direction are mine. I use OpenAI Codex to help implement the software.

The foundation initializes a local SQLite database in WAL mode, applies pending migrations and exits. Run it from the repository root with Python 3.11 or later:

```sh
python3 -m opportunity_beacon
```

Set `BEACON_DB_PATH` in the environment to change the default path, `data/beacon.sqlite3`. With Docker Compose, copy `.env.example` to `.env` for overrides. Keep the database under `/app/data` inside the container so its files use the named volume. Offline builds require a locally available Python base image.

```sh
docker compose build
docker compose up --force-recreate
```

To check persistence, record the migration timestamp, recreate the container and read the same row again. Run the commands in the same Compose project. The volume survives ordinary container removal. The `down -v` option removes it.

```sh
docker compose run --rm beacon python3 -c "import sqlite3; from opportunity_beacon import database_path; print(sqlite3.connect(database_path()).execute('SELECT * FROM schema_migrations').fetchall())"
docker compose up --force-recreate
docker compose run --rm beacon python3 -c "import sqlite3; from opportunity_beacon import database_path; print(sqlite3.connect(database_path()).execute('SELECT * FROM schema_migrations').fetchall())"
```

## Running the tests

CI runs only the offline tests.

From the repository root, create a virtual environment and install the development dependencies. The `--group` option requires pip 25.1 or later.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install "pip>=25.1"
python -m pip install --group dev
python -m pytest -q
```

The persistence test runs offline. It starts the application twice against the same temporary database and checks the saved row, schema version and WAL mode. The application uses only the Python standard library.

MIT license.
