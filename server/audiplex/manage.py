"""Server-box admin CLI: `python -m audiplex.manage <command>`.

Recovery path for a forgotten password when nobody can log in — including
the admin. There is no mail service to send a reset link through, so local
shell access on the server box *is* the authenticating factor here: if you
can run this, you already own the machine and the database file.

    python -m audiplex.manage list-users
    python -m audiplex.manage reset-password admin
    python -m audiplex.manage reset-password admin --password 'hunter2hunter2'

Run it from the server/ directory (same cwd as the server) so the relative
sqlite:// URL in config.yaml resolves to the same database the server uses.
"""

import argparse
import getpass
import sys

from sqlalchemy.orm import sessionmaker

from audiplex.auth import hash_password, verify_password
from audiplex.config import get_settings
from audiplex.database import init_db
from audiplex.models import User

MIN_PASSWORD_LENGTH = 8


def _session():
    """Open a session on the same database the server uses, via the server's
    own init path so any pending migrations are applied identically."""
    settings = get_settings()
    print(f"database: {settings.database_url}", file=sys.stderr)
    engine = init_db(settings.database_url)
    return sessionmaker(bind=engine)()


def cmd_list_users(_args) -> int:
    db = _session()
    try:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("no users in the database")
            return 0
        print(f"{'id':>4}  {'username':<20} {'admin':<6} display_name")
        for u in users:
            print(f"{u.id:>4}  {u.username:<20} {str(bool(u.is_admin)):<6} {u.display_name}")
        return 0
    finally:
        db.close()


def cmd_reset_password(args) -> int:
    db = _session()
    try:
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            names = [u.username for u in db.query(User).order_by(User.id).all()]
            print(f"error: no user named {args.username!r}", file=sys.stderr)
            print(f"known users: {', '.join(names) or '(none)'}", file=sys.stderr)
            return 1

        password = args.password
        if not password:
            password = getpass.getpass(f"new password for {user.username}: ")
            if password != getpass.getpass("confirm: "):
                print("error: passwords did not match", file=sys.stderr)
                return 1

        if len(password) < MIN_PASSWORD_LENGTH:
            print(
                f"error: password must be at least {MIN_PASSWORD_LENGTH} characters",
                file=sys.stderr,
            )
            return 1

        # Hash through the same code path the API uses, so this can never
        # write a hash the server won't accept.
        user.password_hash = hash_password(password)
        db.commit()
        db.refresh(user)

        if not verify_password(password, user.password_hash):
            print("error: wrote a hash that does not verify — nothing to trust here", file=sys.stderr)
            return 1

        print(f"password reset for {user.username!r} (id={user.id}); verified OK")
        print("No server restart needed - the next login will use it.")
        return 0
    finally:
        db.close()


def cmd_tag_repair_dryrun(args) -> int:
    """Report what tag repair would do. Writes nothing to the catalog (#3037)."""
    from audiplex import tag_repair_report

    db = _session()
    try:
        rows = tag_repair_report.collect(db)
        if not rows:
            print("no tracks in the catalog — nothing to weigh")
            return 0

        report = tag_repair_report.render(rows)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(report)
            print(f"report written to {args.out}", file=sys.stderr)
        else:
            print(report)

        if args.export_unresolved:
            count = tag_repair_report.write_export(rows, args.export_unresolved)
            print(
                f"{count} unresolved tracks exported to {args.export_unresolved}",
                file=sys.stderr,
            )
        return 0
    finally:
        db.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m audiplex.manage",
        description="Audiplex server admin commands (run on the server box).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", help="list accounts in the database").set_defaults(
        func=cmd_list_users
    )

    reset = sub.add_parser("reset-password", help="set an account's password")
    reset.add_argument("username", help="account to reset, e.g. admin")
    reset.add_argument(
        "--password",
        help="new password; omit to be prompted (avoids shell history)",
    )
    reset.set_defaults(func=cmd_reset_password)

    dryrun = sub.add_parser(
        "tag-repair-dryrun",
        help="report what tag repair would change; writes nothing",
    )
    dryrun.add_argument("--out", help="write the report here instead of stdout")
    dryrun.add_argument(
        "--export-unresolved",
        help="also write the review bucket as JSON, for the RFL identification pass",
    )
    dryrun.set_defaults(func=cmd_tag_repair_dryrun)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
