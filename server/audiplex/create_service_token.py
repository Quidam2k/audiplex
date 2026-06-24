"""Mint a long-lived JWT for the DJ agent service account.

Creates (or reuses) a non-admin user 'dj-agent' and prints a token the
dedicated audiplex_mcp server uses to authenticate. The token is
independently revocable (delete/disable the dj-agent user) and survives
Todd's personal token rotation — that's why the agent gets its own account
rather than reusing Todd's token (locked decision Q4 auth).

Run from the server/ directory:

    python -m audiplex.create_service_token

Then export the printed values for the MCP server:

    AUDIPLEX_URL=http://<host>:8000
    AUDIPLEX_TOKEN=<printed token>
"""

import secrets

from audiplex.auth import create_token, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db, init_db
from audiplex.models import User

SERVICE_USERNAME = "dj-agent"
TOKEN_HOURS = 87600  # ~10 years — long-lived service credential


def main() -> None:
    settings = get_settings()
    init_db(settings.database_url)

    db_gen = get_db()
    db = next(db_gen)
    try:
        user = db.query(User).filter(User.username == SERVICE_USERNAME).first()
        if user is None:
            user = User(
                username=SERVICE_USERNAME,
                password_hash=hash_password(secrets.token_urlsafe(32)),
                display_name="DJ Agent",
                is_admin=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created service account '{SERVICE_USERNAME}' (id={user.id})")
        else:
            print(f"Reusing service account '{SERVICE_USERNAME}' (id={user.id})")

        token = create_token(user.id, user.username, settings.jwt_secret, TOKEN_HOURS)
        print("\n# Export these for the audiplex_mcp server:")
        print(f"AUDIPLEX_TOKEN={token}")
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


if __name__ == "__main__":
    main()
