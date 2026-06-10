import os
import secrets
import tempfile
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings


class LibraryRoot(BaseModel):
    path: str
    category: str = "audiobook_clean"  # "audiobook_clean" | "audiobook_misc"


class Settings(BaseSettings):
    library_roots: list[LibraryRoot] = Field(default_factory=list)
    library_paths: list[str] = Field(default_factory=list)  # legacy shim
    database_url: str = "sqlite:///./audiplex.db"
    host: str = "0.0.0.0"
    port: int = 8000
    cover_cache_dir: str = "./covers"
    scan_on_startup: bool = True
    apk_output_dir: str = "../android/app/build/outputs/apk/debug"
    jwt_secret: str = ""
    token_expiry_hours: int = 720

    @model_validator(mode="after")
    def _ensure_jwt_secret(self):
        if not self.jwt_secret:
            self.jwt_secret = secrets.token_hex(32)
        return self

    @model_validator(mode="after")
    def _fold_legacy_library_paths(self):
        if not self.library_roots and self.library_paths:
            self.library_roots = [LibraryRoot(path=p) for p in self.library_paths]
        return self


def _load_yaml_config(config_path: Path | None = None) -> dict:
    """Load config.yaml from the given path or default locations."""
    if config_path and config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # Check common locations
    candidates = [
        Path("config.yaml"),
        Path(__file__).parent.parent / "config.yaml",
    ]
    for path in candidates:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    return {}


def _resolve_config_path(config_path: str | None = None) -> Path:
    """The config.yaml to read/write: explicit path, first existing default, else the cwd default."""
    yaml_path = Path(config_path) if config_path else None
    if yaml_path and yaml_path.exists():
        return yaml_path
    candidates = [Path("config.yaml"), Path(__file__).parent.parent / "config.yaml"]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def _write_yaml_atomic(yaml_path: Path, data: dict) -> None:
    """Write config.yaml via a temp file + rename so a crash mid-write can't
    truncate the file (it holds the JWT secret and library roots)."""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(yaml_path.parent) or ".", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
        os.replace(tmp_name, yaml_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _persist_jwt_secret(secret: str, config_path: str | None = None):
    """Write a generated JWT secret back to config.yaml so it survives restarts."""
    yaml_path = _resolve_config_path(config_path)

    data: dict = {}
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    data["jwt_secret"] = secret
    _write_yaml_atomic(yaml_path, data)


def set_library_roots_for_category(
    category: str, paths: list[str], config_path: str | None = None
) -> None:
    """Replace all library roots of one category with `paths`, leaving other
    categories untouched. Persists to config.yaml and clears the settings cache
    so the change takes effect on the next get_settings() without a restart.
    """
    yaml_path = _resolve_config_path(config_path)

    data: dict = {}
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    existing = data.get("library_roots") or []
    kept = [
        r for r in existing
        if not (isinstance(r, dict) and r.get("category") == category)
    ]
    kept.extend({"path": p, "category": category} for p in paths)
    data["library_roots"] = kept

    _write_yaml_atomic(yaml_path, data)

    get_settings.cache_clear()


@lru_cache
def get_settings(config_path: str | None = None) -> Settings:
    """Load settings from config.yaml, with env var overrides."""
    yaml_config = _load_yaml_config(Path(config_path) if config_path else None)
    settings = Settings(**yaml_config)
    if "jwt_secret" not in yaml_config or not yaml_config.get("jwt_secret"):
        _persist_jwt_secret(settings.jwt_secret, config_path)
    return settings
