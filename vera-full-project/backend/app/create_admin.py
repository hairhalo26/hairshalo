"""Create or rotate an admin account — the one that logs into the dashboard.

This exists because `app/seed.py` is the only other code that creates a User,
and it refuses to run outside development. Without this module a correctly
deployed production database has an empty `users` table and nobody can log in
at all: the dashboard is served, the API is healthy, and the login form
rejects every credential because no credential exists.

    # interactive, password never touches argv or shell history
    python -m app.create_admin --email you@hairshalo.com --name "Your Name"

    # non-interactive (CI, or a one-off container task)
    ADMIN_PASSWORD='...' python -m app.create_admin --email you@hairshalo.com \
        --name "Your Name" --from-env

    # rotate the password of an account that already exists
    python -m app.create_admin --email you@hairshalo.com --rotate

The password is read from a prompt or an environment variable, never from a
command-line argument: argv is visible to every process on the box via `ps`,
and lands in shell history and in the container's own command record.
"""
import argparse
import getpass
import os
import re
import sys

from app import models
from app.database import SessionLocal
from app.security import hash_password

MIN_PASSWORD_LENGTH = 12

# The password the development seed uses. It is published in this repository,
# so it must never be accepted here — otherwise this command becomes a way to
# reintroduce exactly what the production preflight refuses to boot with.
SEEDED_PASSWORD = "ChangeMe123!"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def password_problem(password: str) -> str:
    """Why this password is unacceptable, or "" if it is fine.

    Deliberately modest: length carries most of the strength, and a long
    passphrase should not be rejected for lacking a punctuation mark. The
    checks that remain are the ones that catch a genuinely bad choice.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"it must be at least {MIN_PASSWORD_LENGTH} characters"
    if password == SEEDED_PASSWORD:
        return "that is the seed password from this repository"
    if password.lower() in {"password", "admin", "hairshalo", "changeme"}:
        return "it is one of the most-guessed passwords there is"
    if len(set(password)) < 5:
        return "it repeats too few distinct characters"
    return ""


def read_password(from_env: bool) -> str:
    """From ADMIN_PASSWORD, or a prompt. Never from argv."""
    if from_env:
        password = os.getenv("ADMIN_PASSWORD", "")
        if not password:
            sys.exit("--from-env was given but ADMIN_PASSWORD is not set.")
        problem = password_problem(password)
        if problem:
            sys.exit(f"Refusing that ADMIN_PASSWORD: {problem}.")
        return password

    if not sys.stdin.isatty():
        sys.exit(
            "No terminal to prompt on. Pass --from-env and set ADMIN_PASSWORD.\n"
            "With docker compose, add -T:  docker compose ... run --rm -T api ..."
        )

    while True:
        password = getpass.getpass("New admin password: ")
        problem = password_problem(password)
        if problem:
            print(f"  Rejected: {problem}. Try again.", file=sys.stderr)
            continue
        if password != getpass.getpass("Confirm password: "):
            print("  They did not match. Try again.", file=sys.stderr)
            continue
        return password


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or rotate the admin account for the dashboard.")
    parser.add_argument("--email", required=True, help="login address")
    parser.add_argument("--name", default="", help="display name (new accounts)")
    parser.add_argument("--role", default="admin", choices=("admin", "staff"))
    parser.add_argument("--from-env", action="store_true",
                        help="read the password from ADMIN_PASSWORD instead of prompting")
    parser.add_argument("--rotate", action="store_true",
                        help="the account must already exist; change its password")
    args = parser.parse_args(argv)

    email = args.email.strip().lower()
    if not EMAIL_RE.match(email):
        sys.exit(f"{email!r} does not look like an email address.")

    db = SessionLocal()
    try:
        existing = db.query(models.User).filter(models.User.email == email).first()

        if args.rotate and not existing:
            sys.exit(f"No admin account exists for {email}. Drop --rotate to create it.")

        # Changing a password is a different intent from creating an account,
        # and conflating them is how an operator silently resets a colleague's
        # login while thinking they added one.
        if existing and not args.rotate:
            sys.exit(
                f"{email} already exists. Pass --rotate to change its password.")

        password = read_password(args.from_env)

        if existing:
            existing.hashed_password = hash_password(password)
            action = "rotated the password for"
        else:
            db.add(models.User(
                email=email,
                hashed_password=hash_password(password),
                full_name=args.name.strip() or email.split("@")[0],
                role=args.role,
            ))
            action = f"created {args.role} account"

        db.commit()
        print(f"[create_admin] {action} {email}")
        print("[create_admin] Sign in at /admin-dashboard.html")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
