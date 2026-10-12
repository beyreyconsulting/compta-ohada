"""La migration Alembic livrée produit EXACTEMENT le schéma des modèles."""

from pathlib import Path

import pytest

alembic = pytest.importorskip("alembic")
from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402

from sqlalchemy import inspect, text  # noqa: E402

from compta_ohada import metadata  # noqa: E402
from compta_ohada.migrations import DOSSIER_VERSIONS  # noqa: E402


def _config(tmp_path: Path, url: str) -> Config:
    env = tmp_path / "env"
    env.mkdir()
    (env / "script.py.mako").write_text("")
    (env / "env.py").write_text(
        "from alembic import context\n"
        "from sqlalchemy import create_engine\n"
        "url = context.config.get_main_option('sqlalchemy.url')\n"
        "with create_engine(url).connect() as cx:\n"
        "    context.configure(connection=cx, target_metadata=None)\n"
        "    with context.begin_transaction():\n"
        "        context.run_migrations()\n")
    cfg = Config()
    cfg.set_main_option("script_location", str(env))
    cfg.set_main_option("version_locations", DOSSIER_VERSIONS)
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def _purger(engine):
    metadata.drop_all(engine)
    with engine.begin() as cx:
        cx.execute(text("DROP TABLE IF EXISTS alembic_version"))


def test_migration_conforme_aux_modeles(engine, tmp_path):
    _purger(engine)
    cfg = _config(tmp_path, engine.url.render_as_string(hide_password=False))
    try:
        command.upgrade(cfg, "compta_ohada@head")
        with engine.connect() as cx:
            ecarts = compare_metadata(MigrationContext.configure(cx), metadata)
        assert ecarts == []
        command.downgrade(cfg, "compta_ohada@base")
        with engine.connect() as cx:
            assert not [n for n in inspect(cx).get_table_names()
                        if n.startswith("compta_")]
        command.upgrade(cfg, "compta_ohada@head")      # rejouable
    finally:
        _purger(engine)
        metadata.create_all(engine)                    # état attendu par la fixture
