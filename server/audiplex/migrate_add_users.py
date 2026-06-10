"""One-time migration: create admin user and backfill user_id columns.

Run after upgrading to the auth-enabled version:

    cd server
    python -m audiplex.migrate_add_users

The database schema migrations (users table, user_id columns, constraint
changes) run automatically on server startup. This script creates the
initial admin account and backfills existing data with that user's ID.
"""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from audiplex.auth import hash_password
from audiplex.config import get_settings
from audiplex.database import init_db
from audiplex.models import User


def migrate():
    settings = get_settings()
    engine = init_db(settings.database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    existing_admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
    if existing_admin:
        print(f"Admin user already exists: {existing_admin.username}")
        admin = existing_admin
    else:
        password = secrets.token_urlsafe(12)
        admin = User(
            username="admin",
            password_hash=hash_password(password),
            display_name="Admin",
            is_admin=True,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        print("Created admin user:")
        print(f"  Username: admin")
        print(f"  Password: {password}")
        print("  (Save this password — it won't be shown again)")

    tables = ["playback_positions", "playlists", "favorites", "play_stats"]
    with engine.begin() as conn:
        for table in tables:
            result = conn.execute(
                text(f"UPDATE {table} SET user_id = :uid WHERE user_id IS NULL"),
                {"uid": admin.id},
            )
            if result.rowcount:
                print(f"Backfilled {result.rowcount} rows in {table}")

    session.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
