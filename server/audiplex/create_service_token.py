"""Mint a long-lived JWT for the DJ agent service account.

Creates (or reuses) a non-admin user 'dj-agent' and prints a token the
dedicated audiplex_mcp server uses to authenticate. The token is
independently revocable (delete/disable the dj-agent user) and survives
Todd's personal token rotation — that's why the agent gets its own account
rather than reusing Todd's token (locked decision Q4 auth).

Run from the server/ directory:

    python -m audiplex.create_service_token

Pass --username to mint for another service identity, e.g. a Windows
renderer (#2021): `python -m audiplex.create_service_token --username pc-solace`.
Each PC gets its own account so one can be revoked without the others.

Then export the printed values for the MCP server:

    AUDIPLEX_URL=http://<host>:8000
    AUDIPLEX_TOKEN=<printed token>
"""

import argparse
import secrets

from audiplex.auth import create_token, hash_password
from audiplex.config import get_settings
from audiplex.database import get_db, init_db
from audiplex.models import User

SERVICE_USERNAME = "dj-agent"
TOKEN_HOURS = 87600  # ~10 years — long-lived service credential


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", default=SERVICE_USERNAME)
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args()
    username = args.username
    display_name = args.display_name or (
        "DJ Agent" if username == SERVICE_USERNAME else username
    )

    settings = get_settings()
    init_db(settings.database_url)

    db_gen = get_db()
    db = next(db_gen)
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            user = User(
                username=username,
                password_hash=hash_password(secrets.token_urlsafe(32)),
                display_name=display_name,
                is_admin=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created service account '{username}' (id={user.id})")
        else:
            print(f"Reusing service account '{username}' (id={user.id})")

        token = create_token(user.id, user.username, settings.jwt_secret, TOKEN_HOURS)
        print("\n# Token for the service account:")
        print(f"AUDIPLEX_TOKEN={token}")
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass


if __name__ == "__main__":
    main()
