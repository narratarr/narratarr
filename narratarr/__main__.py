import argparse
import getpass
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError, version

import uvicorn


def _version() -> str:
    try:
        return version("narratarr")
    except PackageNotFoundError:
        return "dev"


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    while True:
        first = getpass.getpass("  Password: ")
        if first == getpass.getpass("  Again:    "):
            return first
        print("  Those didn't match. Try again.")


def users_cli(argv: list[str]) -> int:
    """narratarr users …: add people, reset a forgotten password, and so on. Works while Narratarr runs."""
    from . import db, users

    parser = argparse.ArgumentParser(
        prog="narratarr users", description="Manage who can sign in to Narratarr."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="add someone (the first account is an admin)")
    add.add_argument("username")
    add.add_argument(
        "--role",
        choices=users.ROLES,
        help="admin or listener (default: admin for the first account)",
    )
    add.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    sub.add_parser("list", help="list everyone who can sign in")
    passwd = sub.add_parser("passwd", help="set someone's password (signs them out everywhere)")
    passwd.add_argument("username")
    passwd.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    role = sub.add_parser("role", help="make someone an admin or a listener")
    role.add_argument("username")
    role.add_argument("role", choices=users.ROLES)
    remove = sub.add_parser("remove", help="remove someone")
    remove.add_argument("username")
    sub.add_parser(
        "any", help=argparse.SUPPRESS
    )  # exit 0 when an account exists (for the installer)
    args = parser.parse_args(argv)

    db.init()
    if args.cmd == "any":
        return 0 if users.count() else 1
    if args.cmd == "list":
        for u in users.all_users():
            print(f"{u['username']:<32} {u['role']}")
        return 0
    if args.cmd == "add":
        role_ = args.role or ("admin" if users.count() == 0 else "listener")
        try:
            user = users.create(args.username, _read_password(args.password_stdin), role_)
        except users.UserError as e:
            print(f"narratarr: {e}", file=sys.stderr)
            return 1
        print(f"Added {user['username']} ({user['role']}).")
        return 0
    user = users.by_name(args.username)
    if not user:
        print(f"narratarr: there's no user called {args.username}.", file=sys.stderr)
        return 1
    try:
        if args.cmd == "passwd":
            users.set_password(user["id"], _read_password(args.password_stdin))
            print(f"New password set for {user['username']}.")
        elif args.cmd == "role":
            users.set_role(user["id"], args.role)
            print(f"{user['username']} is now a{'n' if args.role == 'admin' else ''} {args.role}.")
        elif args.cmd == "remove":
            users.delete(user["id"])
            print(f"Removed {user['username']}.")
    except users.UserError as e:
        print(f"narratarr: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    if sys.argv[1:2] == ["users"]:
        sys.exit(users_cli(sys.argv[2:]))
    parser = argparse.ArgumentParser(
        prog="narratarr",
        description="Run the Narratarr web app.",
        epilog="Manage accounts with: narratarr users {add,list,passwd,role,remove}",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("NARRATARR_HOST", "127.0.0.1"),
        help="Address to listen on (default: 127.0.0.1, or $NARRATARR_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("NARRATARR_PORT", 8484)),
        help="Port to listen on (default: 8484, or $NARRATARR_PORT)",
    )
    parser.add_argument("--version", action="version", version=f"narratarr {_version()}")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO, and some services (SABnzbd, Prowlarr links) put API keys in URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  Narratarr {_version()} is running → http://{shown}:{args.port}\n")
    uvicorn.run("narratarr.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
