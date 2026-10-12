"""
Fixtures communes.

Chaque test tourne sur SQLite (fichier, WAL, FK actives — exactement comme un
poste boutique). Si la variable d'environnement ``COMPTA_TEST_PG_DSN`` est
définie, il tourne AUSSI sur PostgreSQL (base cloud) :

    COMPTA_TEST_PG_DSN=postgresql+psycopg2://postgres@/compta_test?host=/tmp&port=54329 pytest
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event

from compta_ohada import Comptabilite, creer_schema, metadata

PG_DSN = os.environ.get("COMPTA_TEST_PG_DSN")
MOTEURS = ["sqlite"] + (["postgres"] if PG_DSN else [])


def moteur_sqlite(chemin) -> object:
    engine = create_engine(f"sqlite:///{chemin}",
                           connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def _durcir(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    return engine


@pytest.fixture(params=MOTEURS)
def engine(request, tmp_path):
    if request.param == "sqlite":
        eng = moteur_sqlite(tmp_path / "compta.db")
        creer_schema(eng)
        yield eng
        eng.dispose()
    else:
        eng = create_engine(PG_DSN, pool_pre_ping=True)
        metadata.drop_all(eng)
        creer_schema(eng)
        yield eng
        metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def compta(engine) -> Comptabilite:
    c = Comptabilite(engine, "BOUTIQUE-1", poste="P1", utilisateur="test")
    c.initialiser(exercice=2026)
    return c


def D(valeur) -> Decimal:
    return Decimal(str(valeur)).quantize(Decimal("0.01"))


J = date
