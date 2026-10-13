"""
Tables du cœur comptable (préfixe ``compta_``).

    Tenant ─┬─ Exercice
            ├─ Compte            (plan SYSCOHADA + comptes du tenant)
            ├─ Journal           (VT, AC, BQ, CA, MM, OD, PA, AN)
            ├─ Taxe              (TVA collectée / déductible)
            ├─ Parametre         (clé/valeur : date de verrouillage, devise…)
            ├─ Ecriture ── LigneEcriture   (grand livre, partie double)
            └─ Lettrage ── LigneLettrage

Principes, tous dictés par la synchronisation hors-ligne multi-postes :

1. **Le grand livre est append-only.** Une écriture VALIDÉE n'est plus jamais
   modifiée ni supprimée (garde-fou ORM dans ``garde_fous.py``) ; on la corrige
   par CONTRE-PASSATION. Deux postes hors-ligne ne peuvent donc jamais
   s'écraser mutuellement un solde : un solde est toujours DÉRIVÉ des lignes.
2. **Identifiants déterministes pour les référentiels.** Deux postes qui
   initialisent le même tenant hors-ligne créent le compte 4011 avec le MÊME
   UUID (uuid5) : à la synchronisation, les deux lignes fusionnent au lieu de
   se heurter à l'index unique.
3. **Numérotation par poste.** Une séquence sans trou ne peut être globale
   entre postes déconnectés ; elle l'est par (journal, exercice, poste), et
   chaque séquence porte sa propre chaîne d'empreintes SHA-256.
4. **Pas de clé étrangère vers l'hôte.** ``tenant_id`` est l'identifiant de
   la boutique / de l'école côté ERP, sans contrainte : le paquet ne connaît
   aucun ERP.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, ForeignKey, Index,
                        Integer, String, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .socle import Base, Montant, SyncMixin, ZERO, unique_actif

# Espace de noms des identifiants déterministes (ne JAMAIS changer : tous
# les postes déjà installés en dépendent).
ESPACE_UUID = uuid.UUID("6f1c2a52-8d0e-4c1e-9a4b-0c0a7e3d5b11")


def id_deterministe(tenant_id: str, table: str, cle: str) -> str:
    """UUID v5 stable pour un référentiel : même tenant + même clé naturelle
    => même identifiant sur tous les postes."""
    return str(uuid.uuid5(ESPACE_UUID, f"{tenant_id}|{table}|{cle}"))


# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------

class Exercice(SyncMixin, Base):
    """Exercice comptable (en général l'année civile, art. 7 AUDCIF)."""

    __tablename__ = "compta_exercices"

    OUVERT = "OUVERT"
    CLOTURE = "CLOTURE"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    date_debut: Mapped[date] = mapped_column(Date, nullable=False)
    date_fin: Mapped[date] = mapped_column(Date, nullable=False)
    statut: Mapped[str] = mapped_column(String(10), nullable=False, default=OUVERT)
    cloture_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cloture_par: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        unique_actif("uq_compta_exercices_tenant_code", "tenant_id", "code"),
    )

    def __repr__(self) -> str:
        return f"<Exercice {self.code} {self.statut}>"


class Compte(SyncMixin, Base):
    """Compte du plan comptable d'un tenant."""

    __tablename__ = "compta_comptes"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    numero: Mapped[str] = mapped_column(String(20), nullable=False)
    libelle: Mapped[str] = mapped_column(String(200), nullable=False)
    classe: Mapped[int] = mapped_column(Integer, nullable=False)
    # Compte de détail : peut recevoir des écritures. Les classes (1 chiffre)
    # et rubriques (2 chiffres) sont des regroupements, jamais imputés.
    imputable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Compte de tiers / d'attente que l'on rapproche (factures <-> règlements).
    lettrable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Vient du référentiel SYSCOHADA livré (et non d'une création du tenant).
    standard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Compte tenu en devise (banque en euros, caisse en dollars…) : ses lignes
    # portent le montant en devise et il est réévalué à la clôture.
    devise: Mapped[str | None] = mapped_column(String(3))

    __table_args__ = (
        unique_actif("uq_compta_comptes_tenant_numero", "tenant_id", "numero"),
    )

    def __repr__(self) -> str:
        return f"<Compte {self.numero} {self.libelle!r}>"


class Journal(SyncMixin, Base):
    """Journal comptable."""

    __tablename__ = "compta_journaux"

    VENTE = "VENTE"
    ACHAT = "ACHAT"
    TRESORERIE = "TRESORERIE"
    PAIE = "PAIE"
    OD = "OD"
    A_NOUVEAU = "A_NOUVEAU"
    TYPES = (VENTE, ACHAT, TRESORERIE, PAIE, OD, A_NOUVEAU)

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    libelle: Mapped[str] = mapped_column(String(120), nullable=False)
    type_journal: Mapped[str] = mapped_column(String(12), nullable=False)
    # Pour un journal de trésorerie : le compte de banque / caisse / monnaie
    # électronique qu'il mouvemente (contrepartie des règlements).
    compte_tresorerie: Mapped[str | None] = mapped_column(String(20))
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        unique_actif("uq_compta_journaux_tenant_code", "tenant_id", "code"),
    )

    def __repr__(self) -> str:
        return f"<Journal {self.code}>"


class Taxe(SyncMixin, Base):
    """Taxe sur le chiffre d'affaires (TVA collectée ou déductible)."""

    __tablename__ = "compta_taxes"

    VENTE = "VENTE"
    ACHAT = "ACHAT"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    libelle: Mapped[str] = mapped_column(String(120), nullable=False)
    # Taux en points de base (1800 = 18,00 %) : entier exact, portable.
    taux_pb: Mapped[int] = mapped_column(Integer, nullable=False)
    type_taxe: Mapped[str] = mapped_column(String(10), nullable=False)
    compte: Mapped[str] = mapped_column(String(20), nullable=False)
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        unique_actif("uq_compta_taxes_tenant_code", "tenant_id", "code"),
    )

    @property
    def taux(self) -> Decimal:
        """Taux en pourcentage (Decimal('18.00'))."""
        return (Decimal(self.taux_pb) / Decimal(100)).quantize(Decimal("0.01"))

    def __repr__(self) -> str:
        return f"<Taxe {self.code} {self.taux}%>"


class Parametre(SyncMixin, Base):
    """Paramètre clé/valeur d'un tenant (devise, comptes par défaut…)."""

    __tablename__ = "compta_parametres"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    cle: Mapped[str] = mapped_column(String(60), nullable=False)
    valeur: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        unique_actif("uq_compta_parametres_tenant_cle", "tenant_id", "cle"),
    )


# ---------------------------------------------------------------------------
# Grand livre
# ---------------------------------------------------------------------------

class Ecriture(SyncMixin, Base):
    """Pièce comptable : un ensemble de lignes dont Σ débit = Σ crédit."""

    __tablename__ = "compta_ecritures"

    BROUILLARD = "BROUILLARD"
    VALIDEE = "VALIDEE"

    NORMALE = "NORMALE"
    CONTREPASSATION = "CONTREPASSATION"
    A_NOUVEAU = "A_NOUVEAU"
    CLOTURE = "CLOTURE"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    journal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_journaux.id"), nullable=False)
    journal_code: Mapped[str] = mapped_column(String(8), nullable=False)
    exercice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_exercices.id"), nullable=False, index=True)
    date_ecriture: Mapped[date] = mapped_column(Date, nullable=False)
    libelle: Mapped[str] = mapped_column(String(255), nullable=False)
    # Référence de la pièce justificative (n° de facture, de ticket…).
    reference: Mapped[str | None] = mapped_column(String(60))
    statut: Mapped[str] = mapped_column(String(12), nullable=False,
                                        default=BROUILLARD)
    type_ecriture: Mapped[str] = mapped_column(String(16), nullable=False,
                                               default=NORMALE)
    # Document métier d'origine (idempotence de la génération automatique).
    source_type: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(64))
    # Document métier que cette écriture RÈGLE ou complète (ex. le règlement
    # d'une vente pointe la vente) : sert au lettrage par document et à
    # l'annulation d'un document avec tous ses règlements.
    document_lie: Mapped[str | None] = mapped_column(String(64))
    # Écriture que CELLE-CI contre-passe.
    contrepasse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("compta_ecritures.id"), index=True)
    total: Mapped[Decimal] = mapped_column(Montant, nullable=False, default=ZERO)

    # Numérotation et intégrité (renseignées à la VALIDATION).
    poste: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer)
    numero: Mapped[str | None] = mapped_column(String(60))
    empreinte: Mapped[str | None] = mapped_column(String(64))
    empreinte_precedente: Mapped[str | None] = mapped_column(String(64))
    valide_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valide_par: Mapped[str | None] = mapped_column(String(120))
    cree_par: Mapped[str | None] = mapped_column(String(120))

    # Lignes VIVANTES uniquement (un brouillard modifié tombstone ses
    # anciennes lignes ; elles ne doivent réapparaître nulle part).
    lignes: Mapped[list["LigneEcriture"]] = relationship(
        primaryjoin="and_(LigneEcriture.ecriture_id == Ecriture.id, "
                    "LigneEcriture.deleted_at.is_(None))",
        order_by="LigneEcriture.ordre", viewonly=True)

    __table_args__ = (
        Index("uq_compta_ecritures_sequence", "tenant_id", "journal_code",
              "exercice_id", "poste", "sequence", unique=True,
              sqlite_where=text("sequence IS NOT NULL"),
              postgresql_where=text("sequence IS NOT NULL")),
        Index("ix_compta_ecritures_tenant_date", "tenant_id", "date_ecriture"),
        Index("ix_compta_ecritures_source", "tenant_id", "source_type",
              "source_id"),
        Index("ix_compta_ecritures_document_lie", "tenant_id", "document_lie"),
    )

    @property
    def est_validee(self) -> bool:
        return self.statut == self.VALIDEE

    def __repr__(self) -> str:
        return (f"<Ecriture {self.numero or '(brouillard)'} {self.journal_code} "
                f"{self.date_ecriture} {self.total}>")


class LigneEcriture(SyncMixin, Base):
    """Ligne d'écriture : un compte, un montant au débit OU au crédit."""

    __tablename__ = "compta_lignes"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ecriture_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_ecritures.id"), nullable=False, index=True)
    ordre: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compte_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_comptes.id"), nullable=False)
    # Numéro dénormalisé : états et empreintes ne dépendent pas d'un libellé
    # ou d'un compte renommé plus tard.
    compte_numero: Mapped[str] = mapped_column(String(20), nullable=False)
    libelle: Mapped[str] = mapped_column(String(255), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Montant, nullable=False, default=ZERO)
    credit: Mapped[Decimal] = mapped_column(Montant, nullable=False, default=ZERO)
    # Tiers (client, fournisseur, salarié…) : identifiant côté ERP.
    tiers_ref: Mapped[str | None] = mapped_column(String(64))
    tiers_nom: Mapped[str | None] = mapped_column(String(200))
    date_echeance: Mapped[date | None] = mapped_column(Date)
    # Ligne de taxe : code de la taxe et base hors taxe correspondante.
    taxe_code: Mapped[str | None] = mapped_column(String(20))
    base_taxe: Mapped[Decimal | None] = mapped_column(Montant)
    # Opération en devise : code ISO et montant en devise, du même côté que
    # la ligne (débit/crédit en monnaie de tenue sont la contre-valeur).
    devise: Mapped[str | None] = mapped_column(String(3))
    montant_devise: Mapped[Decimal | None] = mapped_column(Montant)
    # Section analytique (classes 6, 7 et 8 uniquement).
    section: Mapped[str | None] = mapped_column(String(30))

    ecriture: Mapped[Ecriture] = relationship(viewonly=True)

    __table_args__ = (
        Index("ix_compta_lignes_tenant_compte", "tenant_id", "compte_numero"),
        Index("ix_compta_lignes_tenant_tiers", "tenant_id", "tiers_ref"),
        Index("ix_compta_lignes_tenant_section", "tenant_id", "section"),
    )

    @property
    def solde(self) -> Decimal:
        """Débit − crédit."""
        return (self.debit or ZERO) - (self.credit or ZERO)


class Lettrage(SyncMixin, Base):
    """Rapprochement de lignes d'un même compte/tiers dont le solde est nul
    (une facture et ses règlements). Délettrer = tombstone."""

    __tablename__ = "compta_lettrages"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    compte_numero: Mapped[str] = mapped_column(String(20), nullable=False)
    tiers_ref: Mapped[str | None] = mapped_column(String(64))
    cree_par: Mapped[str | None] = mapped_column(String(120))


class LigneLettrage(SyncMixin, Base):
    """Appartenance d'une ligne d'écriture à un lettrage."""

    __tablename__ = "compta_lignes_lettrage"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lettrage_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_lettrages.id"), nullable=False, index=True)
    ligne_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("compta_lignes.id"), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Devises
# ---------------------------------------------------------------------------

class Cours(SyncMixin, Base):
    """Cours d'une devise : valeur d'UNE unité en monnaie de tenue (XOF)."""

    __tablename__ = "compta_cours"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    devise: Mapped[str] = mapped_column(String(3), nullable=False)
    date_cours: Mapped[date] = mapped_column(Date, nullable=False)
    # Cours × 1 000 000 en entier : exact, portable (655,957 -> 655957000).
    cours_micro: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        unique_actif("uq_compta_cours_tenant_devise_date", "tenant_id", "devise",
                     "date_cours"),
    )

    @property
    def cours(self) -> Decimal:
        return Decimal(self.cours_micro) / Decimal(1_000_000)


# ---------------------------------------------------------------------------
# Immobilisations
# ---------------------------------------------------------------------------

class Immobilisation(SyncMixin, Base):
    """Fiche d'immobilisation et paramètres de son plan d'amortissement.

    Les dotations ne sont PAS stockées ici : elles sont dans le grand livre
    (lignes du compte d'amortissement portant ``tiers_ref = code``). Le cumul
    est toujours dérivé, jamais un compteur synchronisé."""

    __tablename__ = "compta_immobilisations"

    LINEAIRE = "LINEAIRE"
    DEGRESSIF = "DEGRESSIF"
    EN_SERVICE = "EN_SERVICE"
    CEDEE = "CEDEE"
    REBUTEE = "REBUTEE"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    libelle: Mapped[str] = mapped_column(String(200), nullable=False)
    compte_immo: Mapped[str] = mapped_column(String(20), nullable=False)
    compte_amort: Mapped[str | None] = mapped_column(String(20))
    compte_dotation: Mapped[str | None] = mapped_column(String(20))
    valeur_origine: Mapped[Decimal] = mapped_column(Montant, nullable=False)
    valeur_residuelle: Mapped[Decimal] = mapped_column(Montant, nullable=False,
                                                       default=ZERO)
    # Amortissements pratiqués AVANT la reprise dans le logiciel (bilan
    # d'ouverture d'un client existant) : déjà inclus dans le solde 28x.
    amortissements_anterieurs: Mapped[Decimal] = mapped_column(
        Montant, nullable=False, default=ZERO)
    date_acquisition: Mapped[date] = mapped_column(Date, nullable=False)
    date_mise_en_service: Mapped[date] = mapped_column(Date, nullable=False)
    duree_mois: Mapped[int] = mapped_column(Integer, nullable=False)
    methode: Mapped[str] = mapped_column(String(10), nullable=False, default=LINEAIRE)
    # Coefficient dégressif × 100 (150 = 1,5).
    coefficient_pc: Mapped[int | None] = mapped_column(Integer)
    statut: Mapped[str] = mapped_column(String(12), nullable=False, default=EN_SERVICE)
    date_sortie: Mapped[date | None] = mapped_column(Date)
    prix_cession: Mapped[Decimal | None] = mapped_column(Montant)

    __table_args__ = (
        unique_actif("uq_compta_immobilisations_tenant_code", "tenant_id", "code"),
    )


# ---------------------------------------------------------------------------
# Comptabilité analytique
# ---------------------------------------------------------------------------

class Section(SyncMixin, Base):
    """Section analytique : activité, rayon, magasin, classe, service…

    ``axe`` regroupe les sections d'une même logique (ex. « ACTIVITE »,
    « MAGASIN ») ; une ligne porte une seule section."""

    __tablename__ = "compta_sections"

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    libelle: Mapped[str] = mapped_column(String(120), nullable=False)
    axe: Mapped[str] = mapped_column(String(30), nullable=False, default="GENERAL")
    actif: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        unique_actif("uq_compta_sections_tenant_code", "tenant_id", "code"),
    )
