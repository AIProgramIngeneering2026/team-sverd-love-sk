import argparse
from bookscout.sqlite_repository import migrate


def main():
    parser = argparse.ArgumentParser(description="Apply BookScout SQLite migrations")
    parser.add_argument("--database", default="bookscout.sqlite3")
    args = parser.parse_args()
    applied = migrate(args.database)
    if applied:
        print("Applied: " + ", ".join(applied))
    else:
        print("Database is up to date")


if __name__ == "__main__":
    main()
