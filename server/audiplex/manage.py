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


def cmd_tag_repair_apply(args) -> int:
    """Repair the music catalog for real, in ONE transaction (#3037).

    Deliberately runs the production scanner rather than a bespoke migration:
    the repair path and the ingest path have to be the same code, or the next
    file Todd drops in gets treated differently from the 217 already here.
    Run the dry run first — `tag-repair-dryrun` — and read it.
    """
    from audiplex.models import Artist, PlayStat, Track, TrackRating, TrackTagRepair
    from audiplex.scanners.music import scan_music

    settings = get_settings()
    roots = [r for r in settings.library_roots if r.category == "music"]
    if not roots:
        print("error: no music root configured", file=sys.stderr)
        return 1

    db = _session()
    try:
        def counts():
            return {
                "tracks": db.query(Track).count(),
                "artists": db.query(Artist).count(),
                "play_stats": db.query(PlayStat).count(),
                "ratings": db.query(TrackRating).count(),
            }

        before = counts()
        print(f"before: {before}")
        if not args.yes:
            print("refusing to write without --yes", file=sys.stderr)
            return 1

        for root in roots:
            result, _ = scan_music(db, root.path, settings.cover_cache_dir)
            for error in result.errors:
                print(f"  warn: {error}", file=sys.stderr)
        db.flush()

        # The nameless artist that owned 207 tracks now owns nothing. Same rule
        # the scan orchestrator uses: an artist with neither albums nor tracks
        # is a leftover, and this one is the very row the repair exists to empty.
        orphans = (
            db.query(Artist)
            .filter(~Artist.albums.any(), ~Artist.tracks.any())
            .all()
        )
        for artist in orphans:
            print(f"  dropping empty artist {artist.name!r}")
            db.delete(artist)

        db.commit()

        after = counts()
        print(f"after : {after}")

        applied = db.query(TrackTagRepair).filter(TrackTagRepair.status == "applied").count()
        held = db.query(TrackTagRepair).filter(TrackTagRepair.status == "pending_review").count()
        print(f"repairs applied: {applied}  held for review: {held}")

        if after["play_stats"] < before["play_stats"]:
            print(
                "ERROR: play statistics were lost — restore the backup",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def cmd_tag_repair_import_rfl(args) -> int:
    """Fold RFL identification verdicts into the repair overlay (#3037 Phase 4)."""
    from audiplex import rfl_import

    db = _session()
    try:
        rows = rfl_import.load(args.path)
        print(f"{len(rows)} verdicts from {args.path}")

        counts, log = rfl_import.import_verdicts(db, rows)
        for line in log:
            print(line)
        print(f"counts: {counts.as_dict()}")

        if args.yes:
            db.commit()
            print("committed")
        else:
            db.rollback()
            print("DRY RUN — nothing written (pass --yes to commit)")
        return 0
    except Exception:
        db.rollback()
        raise
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

    apply_cmd = sub.add_parser(
        "tag-repair-apply",
        help="apply tag repair to the catalog; run the dry run first",
    )
    apply_cmd.add_argument(
        "--yes", action="store_true", help="confirm the write (required)"
    )
    apply_cmd.set_defaults(func=cmd_tag_repair_apply)

    rfl = sub.add_parser(
        "tag-repair-import-rfl",
        help="import RFL identification verdicts into the repair overlay",
    )
    rfl.add_argument("path", help="verdicts JSON from the RFL pass")
    rfl.add_argument("--yes", action="store_true", help="commit (default is a dry run)")
    rfl.set_defaults(func=cmd_tag_repair_import_rfl)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
