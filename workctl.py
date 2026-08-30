#!/usr/bin/env python3
"""WorkOS control CLI."""
import os, argparse
from pathlib import Path
import psycopg
from dotenv import load_dotenv

ROLE = os.environ.get("WORKOS_ROLE", "reader")
load_dotenv(Path(__file__).parent / f".env.{ROLE}")

def cmd_ping(_):
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("select version(), current_user, current_database()")
        version, user, db = cur.fetchone()
        print(f"role : {ROLE}")
        print(f"user : {user}@{db}")
        print(version)

def main():
    p = argparse.ArgumentParser(prog="workctl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping").set_defaults(func=cmd_ping)
    args = p.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
