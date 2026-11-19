from . import database_path, initialize


def main() -> None:
    path = database_path()
    version = initialize(path)
    print(f"Database ready: {path} (schema {version}, WAL)")


if __name__ == "__main__":
    main()
