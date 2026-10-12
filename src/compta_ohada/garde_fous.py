"""
Garde-fou d'inaltérabilité, appliqué au niveau de l'ORM.

Une écriture VALIDÉE et ses lignes sont définitives (art. 19 AUDCIF : les
écritures ne peuvent être ni modifiées ni supprimées une fois validées ; on
corrige par contre-passation). Ce module le garantit pour TOUT code qui passe
par une session SQLAlchemy — y compris un écran de l'ERP écrit maladroitement
ou un script de maintenance — et pas seulement pour l'API du paquet.

Sont tolérées :
- les colonnes purement techniques de synchronisation (``is_synced``,
  ``updated_at``) ;
- une réécriture À L'IDENTIQUE (la synchronisation « last-write-wins » de
  l'hôte réaffecte toutes les colonnes d'une ligne reçue ; si les valeurs sont
  les mêmes, ce n'est pas une modification).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, event, inspect, select
from sqlalchemy.orm import Session

from .erreurs import EcritureVerrouillee
from .modeles import Ecriture, LigneEcriture
from .socle import CENTIME, Montant

COLONNES_TECHNIQUES = frozenset({"is_synced", "updated_at"})


def _normaliser(colonne, valeur):
    """Ramène une valeur à une forme canonique pour comparer « à l'identique »."""
    if valeur is None:
        return None
    type_ = colonne.type
    if isinstance(type_, Montant):
        return Decimal(str(valeur)).quantize(CENTIME)
    if isinstance(type_, DateTime):
        if isinstance(valeur, str):
            valeur = datetime.fromisoformat(valeur)
        if valeur.tzinfo is not None:
            valeur = valeur.astimezone(timezone.utc).replace(tzinfo=None)
        return valeur
    if isinstance(type_, Date):
        if isinstance(valeur, str):
            return date.fromisoformat(valeur)
        if isinstance(valeur, datetime):
            return valeur.date()
        return valeur
    return valeur


def _valeur_engagee(etat, attr: str):
    """Valeur en base (avant les modifications de la session courante)."""
    historique = etat.attrs[attr].history
    if historique.deleted:
        return historique.deleted[0]
    if historique.unchanged:
        return historique.unchanged[0]
    return etat.attrs[attr].loaded_value


def _modifications_reelles(objet) -> list[str]:
    """Colonnes réellement modifiées (hors technique, hors réécriture identique)."""
    etat = inspect(objet)
    modifiees = []
    for colonne in objet.__table__.columns:
        nom = colonne.key
        if nom in COLONNES_TECHNIQUES:
            continue
        historique = etat.attrs[nom].history
        if not historique.has_changes():
            continue
        avant = historique.deleted[0] if historique.deleted else None
        apres = historique.added[0] if historique.added else None
        if _normaliser(colonne, avant) != _normaliser(colonne, apres):
            modifiees.append(nom)
    return modifiees


def _ecriture_validee_en_base(session: Session, ecriture_id: str | None) -> bool:
    """L'écriture parente était-elle DÉJÀ validée avant cette transaction ?"""
    if ecriture_id is None:
        return False
    with session.no_autoflush:
        parent = session.get(Ecriture, ecriture_id,
                             execution_options={"inclure_supprimes": True})
    if parent is None:
        return False
    etat = inspect(parent)
    if etat.pending:
        return False
    return _valeur_engagee(etat, "statut") == Ecriture.VALIDEE


@event.listens_for(Session, "before_flush")
def _proteger_ecritures_validees(session: Session, contexte, instances) -> None:
    for objet in list(session.dirty):
        if isinstance(objet, Ecriture):
            etat = inspect(objet)
            if _valeur_engagee(etat, "statut") != Ecriture.VALIDEE:
                continue
            modifiees = _modifications_reelles(objet)
            if modifiees:
                raise EcritureVerrouillee(
                    f"L'écriture {objet.numero} est validée : modification "
                    f"refusée ({', '.join(sorted(modifiees))}). "
                    "Corrigez-la par contre-passation.")
        elif isinstance(objet, LigneEcriture):
            etat = inspect(objet)
            ecriture_id = _valeur_engagee(etat, "ecriture_id")
            if not _ecriture_validee_en_base(session, ecriture_id):
                continue
            modifiees = _modifications_reelles(objet)
            if modifiees:
                raise EcritureVerrouillee(
                    "Ligne d'une écriture validée : modification refusée "
                    f"({', '.join(sorted(modifiees))}). "
                    "Corrigez par contre-passation.")

    for objet in list(session.deleted):
        if isinstance(objet, Ecriture):
            if _valeur_engagee(inspect(objet), "statut") == Ecriture.VALIDEE:
                raise EcritureVerrouillee(
                    f"L'écriture {objet.numero} est validée : suppression "
                    "refusée. Corrigez-la par contre-passation.")
        elif isinstance(objet, LigneEcriture):
            if _ecriture_validee_en_base(
                    session, _valeur_engagee(inspect(objet), "ecriture_id")):
                raise EcritureVerrouillee(
                    "Ligne d'une écriture validée : suppression refusée.")

    # Ajout de lignes à une écriture déjà validée en base. Cas légitime : la
    # synchronisation reçoit l'en-tête validé AVANT ses lignes (tables
    # poussées dans l'ordre parents -> enfants). Une écriture validée est
    # SCELLÉE par son total : on accepte des lignes tant que leur cumul ne
    # dépasse pas ce total, ce qui interdit tout ajout à une écriture complète.
    nouvelles: dict[str, list[LigneEcriture]] = {}
    for objet in list(session.new):
        if isinstance(objet, LigneEcriture) and objet.deleted_at is None:
            nouvelles.setdefault(objet.ecriture_id, []).append(objet)
    for ecriture_id, lignes in nouvelles.items():
        if not _ecriture_validee_en_base(session, ecriture_id):
            continue
        with session.no_autoflush:
            parent = session.get(Ecriture, ecriture_id,
                                 execution_options={"inclure_supprimes": True})
            ids_nouveaux = {lg.id for lg in lignes if lg.id}
            existantes = [lg for lg in session.scalars(
                select(LigneEcriture).where(
                    LigneEcriture.ecriture_id == ecriture_id,
                    LigneEcriture.deleted_at.is_(None))
                .execution_options(inclure_supprimes=True))
                if lg.id not in ids_nouveaux]
        total = Decimal(str(parent.total))
        cumul_d = sum((Decimal(str(lg.debit or 0)) for lg in existantes + lignes),
                      Decimal(0))
        cumul_c = sum((Decimal(str(lg.credit or 0)) for lg in existantes + lignes),
                      Decimal(0))
        if cumul_d > total or cumul_c > total:
            raise EcritureVerrouillee(
                "Impossible d'ajouter une ligne à une écriture validée.")
