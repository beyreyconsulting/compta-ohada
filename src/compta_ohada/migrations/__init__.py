"""Migrations Alembic du paquet (branche « compta_ohada »).

    from compta_ohada.migrations import DOSSIER_VERSIONS
    # alembic.ini / env.py de l'hôte :
    #   version_locations = <migrations de l'hôte> <DOSSIER_VERSIONS>
    # puis : alembic upgrade heads
"""

from pathlib import Path

DOSSIER_VERSIONS = str(Path(__file__).with_name("versions"))
