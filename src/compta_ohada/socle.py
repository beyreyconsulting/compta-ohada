"""
Socle technique partagé avec les ERP hôtes (StockManager, EcoleManager…).

Repris À L'IDENTIQUE du moteur Offline-First des ERP Beyrey :

- ``Montant``   : centimes en entier (BigInteger), jamais de flottant ;
- ``SyncMixin`` : UUID + is_synced + updated_at + tombstone ``deleted_at`` ;
- ``unique_actif`` : index unique partiel (lignes vivantes seulement) ;
- filtre global des tombstones, avec la même option d'opt-out
  ``inclure_supprimes`` que l'hôte.

Le paquet a SA PROPRE ``MetaData`` : il ne dépend d'aucun ERP. L'hôte ajoute
``compta_ohada.metadata`` à sa cible Alembic (ou appelle ``creer_schema``) et
les tables ``compta_*`` vivent à côté des siennes, dans la même base.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import (BigInteger, Boolean, DateTime, Index, MetaData, String,
                        TypeDecorator, event, text)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            with_loader_criteria)

CENTIME = Decimal("0.01")
ZERO = Decimal("0.00")

# Convention de nommage explicite : indispensable pour qu'Alembic produise
# des noms de contraintes STABLES entre SQLite et PostgreSQL.
CONVENTION_NOMMAGE = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=CONVENTION_NOMMAGE)


def generate_uuid() -> str:
    """UUID v4 en chaîne (portable SQLite/PostgreSQL), généré localement."""
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Horodatage UTC — arbitre les conflits de synchronisation."""
    return datetime.now(timezone.utc)


def montant(valeur, *, champ: str = "montant") -> Decimal:
    """Convertit une saisie (Decimal, int, str) en Decimal exact à 2 décimales.

    Refuse le flottant : ``0.1 + 0.2`` n'a rien à faire dans un grand livre.
    Refuse aussi NaN / infini et les chaînes invalides."""
    if isinstance(valeur, float):
        raise TypeError(
            f"{champ} : flottant refusé ({valeur!r}) — passez un Decimal, "
            "un entier ou une chaîne.")
    if isinstance(valeur, bool) or valeur is None:
        raise TypeError(f"{champ} : valeur manquante ou invalide ({valeur!r}).")
    try:
        d = Decimal(str(valeur).strip().replace(" ", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{champ} : montant illisible ({valeur!r}).") from None
    if not d.is_finite():
        raise ValueError(f"{champ} : montant non fini ({valeur!r}).")
    return d.quantize(CENTIME, rounding=ROUND_HALF_UP)


def unique_actif(nom: str, *colonnes: str) -> Index:
    """Index unique PARTIEL : ne contraint que les lignes vivantes."""
    condition = text("deleted_at IS NULL")
    return Index(nom, *colonnes, unique=True,
                 sqlite_where=condition, postgresql_where=condition)


class Base(DeclarativeBase):
    """Base déclarative du paquet (MetaData propre, séparée de l'hôte)."""

    metadata = metadata


class Montant(TypeDecorator):
    """Type monétaire exact : centimes en BigInteger <-> ``Decimal``."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        arrondi = Decimal(str(value)).quantize(CENTIME, rounding=ROUND_HALF_UP)
        return int(arrondi * 100)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return (Decimal(value) / Decimal(100)).quantize(CENTIME)


class SyncMixin:
    """Colonnes de synchronisation Offline-First (identiques à l'hôte)."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=generate_uuid)
    is_synced: Mapped[bool] = mapped_column(Boolean, default=False,
                                            nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now,
        nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Même option que l'hôte : la synchronisation DOIT voir les tombstones.
INCLURE_SUPPRIMES = {"inclure_supprimes": True}


@event.listens_for(Session, "do_orm_execute")
def _exclure_lignes_supprimees(etat) -> None:
    """Filtre global des tombstones sur les modèles du paquet uniquement.

    S'ajoute (sans le remplacer) au filtre de l'hôte, qui ne vise que SA
    propre classe ``SyncMixin``."""
    if (etat.is_select
            and not etat.is_column_load
            and not etat.is_relationship_load
            and not etat.execution_options.get("inclure_supprimes", False)):
        etat.statement = etat.statement.options(
            with_loader_criteria(
                SyncMixin,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
                track_closure_variables=False,
            ))


def marquer_supprime(*objets) -> None:
    """Suppression logique : tombstone horodaté, remis en file de synchro."""
    for objet in objets:
        objet.deleted_at = utc_now()
        objet.is_synced = False
