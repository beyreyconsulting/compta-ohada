"""
compta-ohada — cœur comptable SYSCOHADA révisé pour les ERP Beyrey Consulting.

    from compta_ohada import Comptabilite, creer_schema, Ligne, Vente, LigneMontant

    creer_schema(engine)                          # ou via Alembic (voir README)
    compta = Comptabilite(engine, tenant_id=boutique_id, poste="CAISSE1",
                          utilisateur="awa")
    compta.initialiser(exercice=2026)             # plan, journaux, TVA 18 %
    compta.comptabiliser_vente(Vente(
        source_id=vente.id, date=vente.date, reference=vente.numero_ticket,
        lignes=[LigneMontant("11800")],           # prix TTC
        reglements=[Reglement("11800", compte="5711")]))
    compta.balance("2026-01-01", "2026-12-31")
"""

from __future__ import annotations

from .analytique import AnalytiqueMixin
from .cloture import ClotureMixin
from .devises import DevisesMixin
from .immobilisations import ImmobilisationsMixin
from .smt import SmtMixin, eligible_smt
from .erreurs import (CompteInconnu, CompteNonImputable, DejaContrepassee,
                      EcritureDesequilibree, EcritureInvalide,
                      EcritureVerrouillee, ErreurComptable, ExerciceClos,
                      ExerciceIntrouvable, IntegriteCompromise, JournalInconnu,
                      LettrageInvalide, ParametreInvalide, PeriodeVerrouillee,
                      TaxeInconnue)
from .etats import EtatsMixin
from .etats_financiers import EtatsFinanciersMixin
from .exports import ExportsMixin
from .evenements import (Achat, BulletinPaie, ChargePatronale, EvenementsMixin,
                         LigneMontant, Reglement, Retenue, Vente)
from .modeles import (Compte, Cours, Ecriture, Immobilisation, Section, Exercice, Journal, Lettrage,
                      LigneEcriture, LigneLettrage, Parametre, Taxe)
from .noyau import Ligne, Noyau
from .socle import Base, Montant, SyncMixin, metadata
from .synchro import TABLES_SYNC, valider_ligne_entrante

__version__ = "0.2.0"


class Comptabilite(EvenementsMixin, DevisesMixin, ImmobilisationsMixin,
                   AnalytiqueMixin, EtatsMixin, EtatsFinanciersMixin, SmtMixin,
                   ExportsMixin, ClotureMixin, Noyau):
    """Point d'entrée : la comptabilité d'UN tenant vue depuis UN poste.

    ``tenant_id`` : identifiant de la boutique / de l'école dans l'ERP.
    ``poste``     : code court et UNIQUE du poste de saisie (préfixe des
                    numéros d'écriture ; indispensable hors-ligne).
    ``utilisateur``: nom de l'utilisateur, tracé sur chaque écriture."""


def creer_schema(engine) -> None:
    """Crée les tables ``compta_*`` (premier lancement, tests). En production,
    préférer les migrations Alembic livrées (voir README)."""
    metadata.create_all(engine)


__all__ = [
    "Comptabilite", "creer_schema", "metadata", "Base", "Montant", "SyncMixin",
    "Cours", "Immobilisation", "Section", "eligible_smt",
    "Ligne", "Vente", "Achat", "LigneMontant", "Reglement", "BulletinPaie",
    "Retenue", "ChargePatronale",
    "Compte", "Ecriture", "Exercice", "Journal", "Lettrage", "LigneEcriture",
    "LigneLettrage", "Parametre", "Taxe", "TABLES_SYNC", "valider_ligne_entrante",
    "ErreurComptable", "CompteInconnu", "CompteNonImputable", "DejaContrepassee",
    "EcritureDesequilibree", "EcritureInvalide", "EcritureVerrouillee",
    "ExerciceClos", "ExerciceIntrouvable", "IntegriteCompromise",
    "JournalInconnu", "LettrageInvalide", "ParametreInvalide",
    "PeriodeVerrouillee", "TaxeInconnue", "__version__",
]
