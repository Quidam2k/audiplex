from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from audiplex.config import get_settings


class Base(DeclarativeBase):
    pass


def get_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    return create_engine(url, connect_args={"check_same_thread": False})


def get_session_factory(engine=None):
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine)


# Default session factory — overridden in tests
_engine = None
_SessionLocal = None


def _migrate_book_category(engine):
    inspector = inspect(engine)
    if "books" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("books")}
    if "category" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE books ADD COLUMN category VARCHAR(50) "
            "NOT NULL DEFAULT 'audiobook_clean'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_books_category ON books (category)"
        ))


def _migrate_chapter_file_path(engine):
    inspector = inspect(engine)
    if "chapters" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("chapters")}
    if "file_path" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE chapters ADD COLUMN file_path TEXT"))


def _migrate_book_series_raw(engine):
    """Add series_raw column and seed it from the old `series` value.

    Before this migration, the scanner stored the raw album tag in `series`.
    After it, `series` holds the parsed series name and `series_raw` keeps
    the original tag so the parser can be re-run on every scan.
    """
    inspector = inspect(engine)
    if "books" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("books")}
    if "series_raw" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE books ADD COLUMN series_raw VARCHAR(500)"))
        conn.execute(text("UPDATE books SET series_raw = series"))


def _migrate_book_series_source(engine):
    """Add series_source column to track where series info came from."""
    inspector = inspect(engine)
    if "books" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("books")}
    if "series_source" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE books ADD COLUMN series_source VARCHAR(20)"))
        conn.execute(text(
            "UPDATE books SET series_source = 'tag' WHERE series IS NOT NULL"
        ))


def _migrate_create_users_table(engine):
    """Create the users table for auth."""
    inspector = inspect(engine)
    if "users" in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users ("
            "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
            "  username VARCHAR(100) NOT NULL UNIQUE,"
            "  password_hash VARCHAR(200) NOT NULL,"
            "  display_name VARCHAR(200) NOT NULL,"
            "  is_admin BOOLEAN DEFAULT 0,"
            "  created_at TIMESTAMP"
            ")"
        ))


def _migrate_add_user_id_columns(engine):
    """Add user_id FK column to tables that need per-user scoping."""
    inspector = inspect(engine)
    targets = ["playback_positions", "playlists", "favorites", "play_stats"]
    for table_name in targets:
        if table_name not in inspector.get_table_names():
            continue
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if "user_id" in cols:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN user_id INTEGER REFERENCES users(id)"
            ))


def _migrate_playback_positions_constraint(engine):
    """Recreate playback_positions with (user_id, book_id) unique constraint."""
    inspector = inspect(engine)
    if "playback_positions" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("playback_positions")}
    if "user_id" not in cols:
        return
    for uc in inspector.get_unique_constraints("playback_positions"):
        if set(uc.get("column_names", [])) == {"user_id", "book_id"}:
            return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE playback_positions RENAME TO _pp_old"))
        conn.execute(text(
            "CREATE TABLE playback_positions ("
            "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
            "  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,"
            "  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,"
            "  position_seconds REAL DEFAULT 0.0,"
            "  chapter_index INTEGER DEFAULT 0,"
            "  updated_at TIMESTAMP,"
            "  is_finished BOOLEAN DEFAULT 0,"
            "  UNIQUE(user_id, book_id)"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO playback_positions"
            "  (id, book_id, user_id, position_seconds, chapter_index, updated_at, is_finished)"
            " SELECT id, book_id, user_id, position_seconds, chapter_index, updated_at, is_finished"
            " FROM _pp_old"
        ))
        conn.execute(text("DROP TABLE _pp_old"))


def _migrate_favorites_constraint(engine):
    """Recreate favorites with (user_id, entity_type, entity_key) unique constraint."""
    inspector = inspect(engine)
    if "favorites" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("favorites")}
    if "user_id" not in cols:
        return
    for uc in inspector.get_unique_constraints("favorites"):
        if set(uc.get("column_names", [])) == {"user_id", "entity_type", "entity_key"}:
            return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE favorites RENAME TO _fav_old"))
        conn.execute(text(
            "CREATE TABLE favorites ("
            "  id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,"
            "  entity_type VARCHAR(20) NOT NULL,"
            "  entity_key VARCHAR(500) NOT NULL,"
            "  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,"
            "  created_at TIMESTAMP,"
            "  UNIQUE(user_id, entity_type, entity_key)"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_favorites_entity_type ON favorites (entity_type)"
        ))
        conn.execute(text(
            "INSERT INTO favorites (id, entity_type, entity_key, user_id, created_at)"
            " SELECT id, entity_type, entity_key, user_id, created_at FROM _fav_old"
        ))
        conn.execute(text("DROP TABLE _fav_old"))


def init_db(database_url: str | None = None):
    """Initialize the database engine and session factory.

    Skips re-initialization if already set up (e.g. in tests).
    """
    global _engine, _SessionLocal
    if _engine is not None:
        Base.metadata.create_all(bind=_engine)
        return _engine
    _engine = get_engine(database_url)
    _SessionLocal = sessionmaker(bind=_engine)
    _migrate_book_category(_engine)
    _migrate_chapter_file_path(_engine)
    _migrate_book_series_raw(_engine)
    _migrate_book_series_source(_engine)
    _migrate_create_users_table(_engine)
    _migrate_add_user_id_columns(_engine)
    _migrate_playback_positions_constraint(_engine)
    _migrate_favorites_constraint(_engine)
    Base.metadata.create_all(bind=_engine)
    return _engine


def get_db():
    """FastAPI dependency that yields a database session."""
    if _SessionLocal is None:
        init_db()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()
