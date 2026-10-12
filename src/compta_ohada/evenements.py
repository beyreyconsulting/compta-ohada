"""
Génération automatique des écritures à partir des événements métier.

C'est L'INTERFACE que chaque ERP appelle. Le paquet ne connaît ni la table
``ventes`` de StockManager ni les ``paiements`` d'EcoleManager : l'ERP décrit
son événement avec les objets ci-dessous, le paquet produit les écritures
SYSCOHADA correspondantes.

Toutes les méthodes sont IDEMPOTENTES (clé : type d'événement + identifiant
du document dans l'ERP) : on peut les rappeler sans crainte après une coupure,
une resynchronisation ou un double clic.

Schémas d'écritures (comptes par défaut, modifiables par paramètre) :

    Vente           D 4111 (TTC)      / C 7011 (HT) + C 4431 (TVA)      [VT]
      règlement     D 5711|5211|552   / C 4111                          [CA|BQ|MM]
    Achat           D 6011 (HT) + D 4452 (TVA) / C 4011 (TTC)           [AC]
      règlement     D 4011            / C 5xx                           [trésorerie]
    Dépense payée   D 6xx (HT) + D 4454 / C 5xx                         [trésorerie]
    Recette diverse D 5xx             / C 7xx (+ C 443x)                [trésorerie]
    Acompte client  D 5xx             / C 4191                          [trésorerie]
      imputation    D 4191            / C 4111                          [OD]
    Paie            D 6611 (brut) / C 422 (net) + C 431, 447x (retenues)
                    + C 421 (avances) ; D 6641 / C 431 (part patronale)  [PA]
      paiement      D 422             / C 5xx                           [trésorerie]
    Avance salarié  D 421             / C 5xx                           [trésorerie]
    Inventaire      D 6031 / C 311 (stock initial) ; D 311 / C 6031 (final) [OD]
    Écart de caisse D 658 / C 571 (manquant) ; D 571 / C 758 (excédent) [trésorerie]
    Virement interne D 5xx (destination) / C 5xx (origine)               [trésorerie]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .erreurs import EcritureInvalide, ParametreInvalide
from .modeles import Ecriture, LigneEcriture, LigneLettrage, Taxe
from .noyau import Ligne, _date, _texte
from .socle import ZERO, montant
from .taxes import ventiler

CLIENTS_DIVERS = ("CLIENTS-DIVERS", "Clients divers")
FOURNISSEURS_DIVERS = ("FOURNISSEURS-DIVERS", "Fournisseurs divers")


# ---------------------------------------------------------------------------
# Objets d'événement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LigneMontant:
    """Ligne d'un document commercial.

    ``montant`` est TTC ou HT selon ``prix_ttc`` du document. Un montant
    négatif représente une remise (le net par taux doit rester positif).
    ``taxe`` : code de taxe ; ``None`` = taxe par défaut du tenant ;
    ``""`` = hors champ de la TVA."""

    montant: Any
    compte: str | None = None
    taxe: str | None = None
    libelle: str | None = None


@dataclass(frozen=True)
class Reglement:
    """Règlement (encaissement ou décaissement) par un compte de trésorerie."""

    montant: Any
    compte: str = "5711"
    date: date | None = None
    reference: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class Vente:
    source_id: str
    date: date
    lignes: list[LigneMontant]
    tiers_ref: str | None = None
    tiers_nom: str | None = None
    reference: str | None = None
    libelle: str | None = None
    prix_ttc: bool = True
    date_echeance: date | None = None
    reglements: list[Reglement] = field(default_factory=list)


@dataclass(frozen=True)
class Achat:
    source_id: str
    date: date
    lignes: list[LigneMontant]
    tiers_ref: str | None = None
    tiers_nom: str | None = None
    reference: str | None = None
    libelle: str | None = None
    prix_ttc: bool = False
    date_echeance: date | None = None
    reglements: list[Reglement] = field(default_factory=list)


@dataclass(frozen=True)
class Retenue:
    """Retenue sur salaire (part salariale INPS/AMO, ITS, opposition…)."""

    libelle: str
    montant: Any
    compte: str


@dataclass(frozen=True)
class ChargePatronale:
    """Cotisation à la charge de l'employeur."""

    libelle: str
    montant: Any
    compte_organisme: str = "431"
    compte_charge: str | None = None


@dataclass(frozen=True)
class BulletinPaie:
    source_id: str
    date: date
    salarie_ref: str
    salarie_nom: str
    brut: Any
    retenues: list[Retenue] = field(default_factory=list)
    charges_patronales: list[ChargePatronale] = field(default_factory=list)
    avances_deduites: Any = 0
    compte_brut: str | None = None
    reference: str | None = None
    libelle: str | None = None


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class EvenementsMixin:
    """Méthodes ``comptabiliser_*`` de ``Comptabilite``."""

    # -- utilitaires ----------------------------------------------------

    def _taxes_tenant(self, s: Session, type_taxe: str) -> dict[str, tuple[Decimal, str]]:
        return {t.code: (t.taux, t.compte) for t in s.scalars(select(Taxe).where(
            Taxe.tenant_id == self.tenant_id, Taxe.type_taxe == type_taxe,
            Taxe.actif.is_(True)))}

    def _code_taxe(self, s: Session, demande: str | None, type_taxe: str) -> str | None:
        if self._parametre(s, "assujetti_tva") != "1":
            return None
        if demande == "":
            return None
        cle = "taxe_vente_defaut" if type_taxe == Taxe.VENTE else "taxe_achat_defaut"
        code = (demande or self._parametre(s, cle) or "").strip().upper()
        if not code:
            return None
        taxe = self._taxe(s, code)
        if taxe.type_taxe != type_taxe:
            raise ParametreInvalide(
                f"La taxe {code} est une taxe {taxe.type_taxe}, pas {type_taxe}.")
        return code

    def _lignes_document(self, s: Session, lignes: list[LigneMontant],
                         type_taxe: str, compte_defaut_param: str, prix_ttc: bool):
        if not lignes:
            raise EcritureInvalide("Document sans ligne.")
        brutes = []
        for i, lg in enumerate(lignes, 1):
            m = montant(lg.montant, champ=f"ligne {i}")
            if m == ZERO:
                continue
            compte = lg.compte or self._parametre(s, compte_defaut_param)
            brutes.append((str(compte), self._code_taxe(s, lg.taxe, type_taxe), m))
        if not brutes:
            raise EcritureInvalide("Document de montant nul.")
        taxes = self._taxes_tenant(s, type_taxe)
        ventilees, taxes_v = ventiler(brutes, taxes, prix_ttc=prix_ttc)
        # Agrégation par (compte, taxe) : une ligne d'écriture par couple.
        par_compte: dict[tuple[str, str | None], Decimal] = {}
        for v in ventilees:
            cle = (v.compte, v.taxe_code)
            par_compte[cle] = par_compte.get(cle, ZERO) + v.ht
        total_ttc = sum((v.ttc for v in ventilees), ZERO)
        if total_ttc <= ZERO:
            raise EcritureInvalide("Le total du document doit être positif.")
        return par_compte, taxes_v, total_ttc

    @staticmethod
    def _sens(m: Decimal, *, au_credit: bool) -> dict:
        """Place un montant signé du bon côté (un négatif change de côté)."""
        if (m >= 0) == au_credit:
            return {"credit": abs(m)}
        return {"debit": abs(m)}

    def _journal_de(self, s: Session, compte_tresorerie: str) -> str:
        return self.journal_tresorerie_pour(compte_tresorerie, session=s)

    def _lettrer_document(self, s: Session, compte: str, tiers_ref: str | None,
                          document_id: str, types: tuple[str, ...]) -> str | None:
        """Lettre les lignes ouvertes d'un document et de ses règlements si
        leur solde est nul (paiement complet)."""
        lettrees = select(LigneLettrage.ligne_id).where(
            LigneLettrage.tenant_id == self.tenant_id,
            LigneLettrage.deleted_at.is_(None))
        lignes = list(s.scalars(
            select(LigneEcriture)
            .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
            .where(LigneEcriture.tenant_id == self.tenant_id,
                   LigneEcriture.compte_numero == compte,
                   (LigneEcriture.tiers_ref == tiers_ref) if tiers_ref is not None
                   else LigneEcriture.tiers_ref.is_(None),
                   Ecriture.statut == Ecriture.VALIDEE,
                   Ecriture.type_ecriture != Ecriture.CONTREPASSATION,
                   Ecriture.exercice_id.in_(self._exercices_ouverts(s)),
                   or_(Ecriture.source_id == document_id,
                       Ecriture.document_lie == document_id),
                   Ecriture.source_type.in_(types),
                   LigneEcriture.id.not_in(lettrees))))
        lignes = [lg for lg in lignes if not self._est_contrepassee(s, lg.ecriture_id)]
        if len(lignes) >= 2 and sum((lg.solde for lg in lignes), ZERO) == ZERO:
            return self._lettrer(s, [lg.id for lg in lignes])
        return None

    @staticmethod
    def _source_reglement(document_id: str, i: int, reglement: Reglement) -> str:
        return reglement.source_id or f"{document_id}#{i}"

    # -- ventes ---------------------------------------------------------

    def comptabiliser_vente(self, vente: Vente, *,
                            session: Session | None = None) -> dict:
        """Facture (journal des ventes) + règlements immédiats (trésorerie).

        Renvoie ``{"facture": écriture, "reglements": [écritures], "lettrage": code|None}``."""
        source_id = _texte(vente.source_id, champ="identifiant de vente", max_len=60)
        jour = _date(vente.date)
        tiers_ref = vente.tiers_ref or CLIENTS_DIVERS[0]
        tiers_nom = vente.tiers_nom or (CLIENTS_DIVERS[1] if not vente.tiers_ref else None)

        def _t(s: Session) -> dict:
            compte_client = self._parametre(s, "compte_client")
            par_compte, taxes_v, total = self._lignes_document(
                s, vente.lignes, Taxe.VENTE, "compte_vente_marchandises",
                vente.prix_ttc)
            libelle = vente.libelle or (
                f"Vente {vente.reference}" if vente.reference else "Vente")
            lignes = [Ligne(compte=compte_client, debit=total, libelle=libelle,
                            tiers_ref=tiers_ref, tiers_nom=tiers_nom,
                            date_echeance=vente.date_echeance)]
            for (compte, code), ht in sorted(par_compte.items(),
                                             key=lambda x: (x[0][0], x[0][1] or "")):
                if ht == ZERO:
                    continue
                lignes.append(Ligne(compte=compte, libelle=libelle, taxe_code=code,
                                    **self._sens(ht, au_credit=True)))
            for tv in taxes_v:
                if tv.tva == ZERO:
                    continue
                lignes.append(Ligne(compte=tv.compte, credit=tv.tva,
                                    libelle=f"TVA {tv.taux} % — {libelle}",
                                    taxe_code=tv.code, base_taxe=tv.base))
            facture = self._saisir(
                self._parametre(s, "journal_ventes"), jour, libelle, lignes,
                reference=vente.reference, valider=True, source_type="vente",
                source_id=source_id, type_ecriture=Ecriture.NORMALE, session=s)
            reglements = self._reglements(
                s, vente.reglements, document_id=source_id, jour=jour,
                compte_tiers=compte_client, tiers_ref=tiers_ref,
                tiers_nom=tiers_nom, encaissement=True, source_type="vente.reglement",
                libelle=libelle, reference=vente.reference, plafond=facture["total"])
            code = self._lettrer_document(s, compte_client, tiers_ref, source_id,
                                          ("vente", "vente.reglement"))
            return {"facture": facture, "reglements": reglements, "lettrage": code}

        return self._executer_avec_reprise_numerotation(_t, session)

    def _reglements(self, s: Session, reglements: list[Reglement], *,
                    document_id: str, jour: date, compte_tiers: str,
                    tiers_ref: str | None, tiers_nom: str | None,
                    encaissement: bool, source_type: str, libelle: str,
                    reference: str | None, plafond: Decimal | None) -> list[dict]:
        resultats = []
        cumul = ZERO
        for i, r in enumerate(reglements, 1):
            m = montant(r.montant, champ="règlement")
            if m <= ZERO:
                raise EcritureInvalide("Un règlement est strictement positif.")
            cumul += m
            if plafond is not None and cumul > plafond:
                raise EcritureInvalide(
                    f"Règlements ({cumul}) supérieurs au montant du document "
                    f"({plafond}). La monnaie rendue ne se comptabilise pas.")
            resultats.append(self._reglement(
                s, montant_=m, compte_tresorerie=r.compte,
                jour=_date(r.date) if r.date else jour, compte_tiers=compte_tiers,
                tiers_ref=tiers_ref, tiers_nom=tiers_nom, encaissement=encaissement,
                source_type=source_type,
                source_id=self._source_reglement(document_id, i, r),
                document_lie=document_id,
                libelle=f"Règlement {libelle}", reference=r.reference or reference))
        return resultats

    def _reglement(self, s: Session, *, montant_: Decimal, compte_tresorerie: str,
                   jour: date, compte_tiers: str, tiers_ref, tiers_nom,
                   encaissement: bool, source_type: str, source_id: str,
                   document_lie: str | None, libelle: str,
                   reference: str | None) -> dict:
        tresorerie = Ligne(compte=compte_tresorerie, libelle=libelle,
                           **({"debit": montant_} if encaissement
                              else {"credit": montant_}))
        tiers = Ligne(compte=compte_tiers, libelle=libelle, tiers_ref=tiers_ref,
                      tiers_nom=tiers_nom,
                      **({"credit": montant_} if encaissement
                         else {"debit": montant_}))
        return self._saisir(self._journal_de(s, compte_tresorerie), jour, libelle,
                            [tresorerie, tiers], reference=reference, valider=True,
                            source_type=source_type, source_id=source_id,
                            document_lie=document_lie,
                            type_ecriture=Ecriture.NORMALE, session=s)

    def comptabiliser_reglement_client(
            self, source_id: str, date_reglement, montant_, *, vente_source_id:
            str | None = None, tiers_ref: str | None = None,
            tiers_nom: str | None = None, compte: str = "5711",
            reference: str | None = None,
            session: Session | None = None) -> dict:
        """Encaissement d'une créance client (vente à crédit, partielle).

        Avec ``vente_source_id``, le règlement est rattaché à la vente et le
        lettrage se fait dès que la vente est soldée."""
        source_id = _texte(source_id, champ="identifiant du règlement", max_len=64)
        jour = _date(date_reglement)
        m = montant(montant_, champ="règlement")
        if m <= ZERO:
            raise EcritureInvalide("Un règlement est strictement positif.")
        tiers_ref = tiers_ref or CLIENTS_DIVERS[0]

        def _t(s: Session) -> dict:
            compte_client = self._parametre(s, "compte_client")
            ecr = self._reglement(
                s, montant_=m, compte_tresorerie=compte, jour=jour,
                compte_tiers=compte_client, tiers_ref=tiers_ref,
                tiers_nom=tiers_nom, encaissement=True,
                source_type="vente.reglement", source_id=source_id,
                document_lie=vente_source_id,
                libelle=f"Règlement client {reference or tiers_nom or tiers_ref}",
                reference=reference)
            code = None
            if vente_source_id:
                code = self._lettrer_document(s, compte_client, tiers_ref,
                                              vente_source_id,
                                              ("vente", "vente.reglement"))
            if code is None:
                # Créance reprise en à-nouveau, ou règlement global du compte.
                code = self._lettrer_si_solde(s, compte_client, tiers_ref)
            return {"reglement": ecr, "lettrage": code}
        return self._executer_avec_reprise_numerotation(_t, session)

    # -- achats ---------------------------------------------------------

    def comptabiliser_achat(self, achat: Achat, *,
                            session: Session | None = None) -> dict:
        """Facture fournisseur (journal des achats) + règlements immédiats."""
        source_id = _texte(achat.source_id, champ="identifiant d'achat", max_len=60)
        jour = _date(achat.date)
        tiers_ref = achat.tiers_ref or FOURNISSEURS_DIVERS[0]
        tiers_nom = achat.tiers_nom or (
            FOURNISSEURS_DIVERS[1] if not achat.tiers_ref else None)

        def _t(s: Session) -> dict:
            compte_fournisseur = self._parametre(s, "compte_fournisseur")
            par_compte, taxes_v, total = self._lignes_document(
                s, achat.lignes, Taxe.ACHAT, "compte_achat_marchandises",
                achat.prix_ttc)
            libelle = achat.libelle or (
                f"Achat {achat.reference}" if achat.reference else "Achat")
            lignes = []
            for (compte, code), ht in sorted(par_compte.items(),
                                             key=lambda x: (x[0][0], x[0][1] or "")):
                if ht == ZERO:
                    continue
                lignes.append(Ligne(compte=compte, libelle=libelle, taxe_code=code,
                                    **self._sens(ht, au_credit=False)))
            for tv in taxes_v:
                if tv.tva == ZERO:
                    continue
                lignes.append(Ligne(compte=tv.compte, debit=tv.tva,
                                    libelle=f"TVA {tv.taux} % — {libelle}",
                                    taxe_code=tv.code, base_taxe=tv.base))
            lignes.append(Ligne(compte=compte_fournisseur, credit=total,
                                libelle=libelle, tiers_ref=tiers_ref,
                                tiers_nom=tiers_nom,
                                date_echeance=achat.date_echeance))
            facture = self._saisir(
                self._parametre(s, "journal_achats"), jour, libelle, lignes,
                reference=achat.reference, valider=True, source_type="achat",
                source_id=source_id, type_ecriture=Ecriture.NORMALE, session=s)
            reglements = self._reglements(
                s, achat.reglements, document_id=source_id, jour=jour,
                compte_tiers=compte_fournisseur, tiers_ref=tiers_ref,
                tiers_nom=tiers_nom, encaissement=False,
                source_type="achat.reglement", libelle=libelle,
                reference=achat.reference, plafond=facture["total"])
            code = self._lettrer_document(s, compte_fournisseur, tiers_ref,
                                          source_id, ("achat", "achat.reglement"))
            return {"facture": facture, "reglements": reglements, "lettrage": code}

        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_reglement_fournisseur(
            self, source_id: str, date_reglement, montant_, *,
            achat_source_id: str | None = None, tiers_ref: str | None = None,
            tiers_nom: str | None = None, compte: str = "5711",
            reference: str | None = None,
            session: Session | None = None) -> dict:
        source_id = _texte(source_id, champ="identifiant du règlement", max_len=64)
        jour = _date(date_reglement)
        m = montant(montant_, champ="règlement")
        if m <= ZERO:
            raise EcritureInvalide("Un règlement est strictement positif.")
        tiers_ref = tiers_ref or FOURNISSEURS_DIVERS[0]

        def _t(s: Session) -> dict:
            compte_fournisseur = self._parametre(s, "compte_fournisseur")
            ecr = self._reglement(
                s, montant_=m, compte_tresorerie=compte, jour=jour,
                compte_tiers=compte_fournisseur, tiers_ref=tiers_ref,
                tiers_nom=tiers_nom, encaissement=False,
                source_type="achat.reglement", source_id=source_id,
                document_lie=achat_source_id,
                libelle=f"Règlement fournisseur {reference or tiers_nom or tiers_ref}",
                reference=reference)
            code = None
            if achat_source_id:
                code = self._lettrer_document(s, compte_fournisseur, tiers_ref,
                                              achat_source_id,
                                              ("achat", "achat.reglement"))
            if code is None:
                code = self._lettrer_si_solde(s, compte_fournisseur, tiers_ref)
            return {"reglement": ecr, "lettrage": code}
        return self._executer_avec_reprise_numerotation(_t, session)

    # -- trésorerie directe --------------------------------------------

    def comptabiliser_depense(self, source_id: str, date_depense, montant_, *,
                              compte_charge: str, libelle: str,
                              compte_tresorerie: str = "5711",
                              taxe: str | None = "", prix_ttc: bool = True,
                              tiers_ref: str | None = None,
                              tiers_nom: str | None = None,
                              reference: str | None = None,
                              session: Session | None = None) -> dict:
        """Dépense payée comptant (loyer, électricité, transport…).

        Par défaut sans TVA déductible (``taxe=""``) : la plupart des
        dépenses courantes d'un petit commerce n'ont pas de facture conforme
        ouvrant droit à déduction. Passez ``taxe="TVA18-FG"`` sinon."""
        source_id = _texte(source_id, champ="identifiant de dépense", max_len=64)
        jour = _date(date_depense)
        libelle = _texte(libelle, champ="libellé", max_len=255)

        def _t(s: Session) -> dict:
            par_compte, taxes_v, total = self._lignes_document(
                s, [LigneMontant(montant_, compte=compte_charge, taxe=taxe)],
                Taxe.ACHAT, "compte_achat_marchandises", prix_ttc)
            lignes = [Ligne(compte=c, libelle=libelle, taxe_code=code,
                            tiers_ref=tiers_ref, tiers_nom=tiers_nom,
                            **self._sens(ht, au_credit=False))
                      for (c, code), ht in par_compte.items() if ht != ZERO]
            lignes += [Ligne(compte=tv.compte, debit=tv.tva,
                             libelle=f"TVA {tv.taux} % — {libelle}",
                             taxe_code=tv.code, base_taxe=tv.base)
                       for tv in taxes_v if tv.tva != ZERO]
            lignes.append(Ligne(compte=compte_tresorerie, credit=total,
                                libelle=libelle))
            return self._saisir(self._journal_de(s, compte_tresorerie), jour,
                                libelle, lignes, reference=reference,
                                valider=True, source_type="depense",
                                source_id=source_id,
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_recette(self, source_id: str, date_recette, montant_, *,
                              compte_produit: str, libelle: str,
                              compte_tresorerie: str = "5711",
                              taxe: str | None = "", prix_ttc: bool = True,
                              reference: str | None = None,
                              session: Session | None = None) -> dict:
        """Recette encaissée hors vente (location, commission, subvention…)."""
        source_id = _texte(source_id, champ="identifiant de recette", max_len=64)
        jour = _date(date_recette)
        libelle = _texte(libelle, champ="libellé", max_len=255)

        def _t(s: Session) -> dict:
            par_compte, taxes_v, total = self._lignes_document(
                s, [LigneMontant(montant_, compte=compte_produit, taxe=taxe)],
                Taxe.VENTE, "compte_vente_services", prix_ttc)
            lignes = [Ligne(compte=compte_tresorerie, debit=total, libelle=libelle)]
            lignes += [Ligne(compte=c, libelle=libelle, taxe_code=code,
                             **self._sens(ht, au_credit=True))
                       for (c, code), ht in par_compte.items() if ht != ZERO]
            lignes += [Ligne(compte=tv.compte, credit=tv.tva,
                             libelle=f"TVA {tv.taux} % — {libelle}",
                             taxe_code=tv.code, base_taxe=tv.base)
                       for tv in taxes_v if tv.tva != ZERO]
            return self._saisir(self._journal_de(s, compte_tresorerie), jour,
                                libelle, lignes, reference=reference,
                                valider=True, source_type="recette",
                                source_id=source_id,
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_virement_interne(self, source_id: str, date_virement,
                                       montant_, *, de: str, vers: str,
                                       libelle: str = "Virement interne",
                                       session: Session | None = None) -> dict:
        """Transfert entre deux comptes de trésorerie (versement de la caisse
        en banque, alimentation du compte Orange Money…)."""
        m = montant(montant_, champ="montant")
        if m <= ZERO:
            raise EcritureInvalide("Un virement est strictement positif.")
        if str(de) == str(vers):
            raise EcritureInvalide("Comptes d'origine et de destination identiques.")
        jour = _date(date_virement)

        def _t(s: Session) -> dict:
            for c in (de, vers):
                if self._compte(s, c, pour_imputation=True).classe != 5:
                    raise EcritureInvalide(
                        "Un virement interne relie deux comptes de trésorerie.")
            return self._saisir(
                self._journal_de(s, de), jour, libelle,
                [Ligne(compte=vers, debit=m), Ligne(compte=de, credit=m)],
                reference=None, valider=True, source_type="virement",
                source_id=_texte(source_id, champ="identifiant", max_len=64),
                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_ecart_caisse(self, source_id: str, date_ecart, ecart, *,
                                   compte_caisse: str = "5711",
                                   libelle: str | None = None,
                                   session: Session | None = None) -> dict | None:
        """Écart constaté à la clôture de caisse (compté − théorique).

        Négatif : manquant (charge 658) ; positif : excédent (produit 758)."""
        e = montant(ecart, champ="écart")
        if e == ZERO:
            return None
        jour = _date(date_ecart)

        def _t(s: Session) -> dict:
            if e < ZERO:
                lib = libelle or "Manquant de caisse"
                lignes = [Ligne(compte=self._parametre(s, "compte_ecart_caisse_perte"),
                                debit=-e), Ligne(compte=compte_caisse, credit=-e)]
            else:
                lib = libelle or "Excédent de caisse"
                lignes = [Ligne(compte=compte_caisse, debit=e),
                          Ligne(compte=self._parametre(s, "compte_ecart_caisse_gain"),
                                credit=e)]
            return self._saisir(self._journal_de(s, compte_caisse), jour, lib,
                                lignes, reference=None, valider=True,
                                source_type="ecart_caisse",
                                source_id=_texte(source_id, champ="identifiant",
                                                 max_len=64),
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    # -- acomptes -------------------------------------------------------

    def comptabiliser_acompte_client(self, source_id: str, date_acompte, montant_,
                                     *, tiers_ref: str, tiers_nom: str | None = None,
                                     compte: str = "5711",
                                     reference: str | None = None,
                                     session: Session | None = None) -> dict:
        """Avance reçue d'un client avant facturation (D 5xx / C 4191)."""
        m = montant(montant_, champ="acompte")
        if m <= ZERO:
            raise EcritureInvalide("Un acompte est strictement positif.")
        jour = _date(date_acompte)

        def _t(s: Session) -> dict:
            lib = f"Acompte client {reference or tiers_nom or tiers_ref}"
            return self._saisir(
                self._journal_de(s, compte), jour, lib,
                [Ligne(compte=compte, debit=m),
                 Ligne(compte=self._parametre(s, "compte_acompte_client"), credit=m,
                       tiers_ref=tiers_ref, tiers_nom=tiers_nom)],
                reference=reference, valider=True, source_type="acompte",
                source_id=_texte(source_id, champ="identifiant", max_len=64),
                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    def imputer_acompte_client(self, source_id: str, date_imputation, montant_, *,
                               tiers_ref: str, vente_source_id: str,
                               tiers_nom: str | None = None,
                               session: Session | None = None) -> dict:
        """Impute un acompte sur une vente facturée (D 4191 / C 4111)."""
        m = montant(montant_, champ="montant imputé")
        if m <= ZERO:
            raise EcritureInvalide("Le montant imputé est strictement positif.")
        jour = _date(date_imputation)

        def _t(s: Session) -> dict:
            compte_client = self._parametre(s, "compte_client")
            lib = "Imputation d'acompte"
            ecr = self._saisir(
                self._parametre(s, "journal_od"), jour, lib,
                [Ligne(compte=self._parametre(s, "compte_acompte_client"), debit=m,
                       tiers_ref=tiers_ref, tiers_nom=tiers_nom),
                 Ligne(compte=compte_client, credit=m, tiers_ref=tiers_ref,
                       tiers_nom=tiers_nom)],
                reference=None, valider=True, source_type="vente.acompte",
                source_id=_texte(source_id, champ="identifiant", max_len=64),
                document_lie=vente_source_id,
                type_ecriture=Ecriture.NORMALE, session=s)
            code = self._lettrer_document(
                s, compte_client, tiers_ref, vente_source_id,
                ("vente", "vente.reglement", "vente.acompte"))
            return {"imputation": ecr, "lettrage": code}
        return self._executer_avec_reprise_numerotation(_t, session)

    # -- paie -----------------------------------------------------------

    def comptabiliser_paie(self, bulletin: BulletinPaie, *,
                           session: Session | None = None) -> dict:
        """Bulletin de paie : charge brute, retenues, net à payer, part patronale."""
        source_id = _texte(bulletin.source_id, champ="identifiant du bulletin",
                           max_len=64)
        jour = _date(bulletin.date)
        brut = montant(bulletin.brut, champ="salaire brut")
        if brut <= ZERO:
            raise EcritureInvalide("Le salaire brut est strictement positif.")
        retenues = [(r, montant(r.montant, champ=f"retenue {r.libelle}"))
                    for r in bulletin.retenues]
        patronales = [(c, montant(c.montant, champ=f"charge {c.libelle}"))
                      for c in bulletin.charges_patronales]
        avances = montant(bulletin.avances_deduites or 0, champ="avances")
        if any(m < ZERO for _, m in retenues + patronales) or avances < ZERO:
            raise EcritureInvalide("Retenues, charges et avances sont positives.")
        net = brut - sum((m for _, m in retenues), ZERO) - avances
        if net < ZERO:
            raise EcritureInvalide(
                f"Net à payer négatif ({net}) : retenues et avances dépassent le brut.")

        def _t(s: Session) -> dict:
            lib = bulletin.libelle or f"Paie {bulletin.salarie_nom}"
            ref, nom = bulletin.salarie_ref, bulletin.salarie_nom
            lignes = [Ligne(compte=bulletin.compte_brut or self._parametre(
                s, "compte_salaires"), debit=brut, libelle=f"Salaire brut — {nom}")]
            for r, m in retenues:
                if m:
                    lignes.append(Ligne(compte=r.compte, credit=m,
                                        libelle=f"{r.libelle} — {nom}",
                                        tiers_ref=ref, tiers_nom=nom))
            if avances:
                lignes.append(Ligne(compte=self._parametre(s, "compte_avances_personnel"),
                                    credit=avances, libelle=f"Avances déduites — {nom}",
                                    tiers_ref=ref, tiers_nom=nom))
            if net:
                lignes.append(Ligne(compte=self._parametre(s, "compte_remunerations_dues"),
                                    credit=net, libelle=f"Net à payer — {nom}",
                                    tiers_ref=ref, tiers_nom=nom))
            for c, m in patronales:
                if not m:
                    continue
                lignes.append(Ligne(compte=c.compte_charge or self._parametre(
                    s, "compte_charges_sociales"), debit=m,
                    libelle=f"{c.libelle} (part patronale) — {nom}"))
                lignes.append(Ligne(compte=c.compte_organisme, credit=m,
                                    libelle=f"{c.libelle} (part patronale) — {nom}"))
            ecr = self._saisir(self._parametre(s, "journal_paie"), jour, lib, lignes,
                               reference=bulletin.reference, valider=True,
                               source_type="paie", source_id=source_id,
                               type_ecriture=Ecriture.NORMALE, session=s)
            if avances:
                self._lettrer_si_solde(
                    s, self._parametre(s, "compte_avances_personnel"), ref)
            return {"bulletin": ecr, "net_a_payer": net}
        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_paiement_salaire(self, source_id: str, date_paiement,
                                       montant_, *, salarie_ref: str,
                                       salarie_nom: str | None = None,
                                       bulletin_source_id: str | None = None,
                                       compte: str = "5711",
                                       session: Session | None = None) -> dict:
        m = montant(montant_, champ="montant")
        if m <= ZERO:
            raise EcritureInvalide("Un paiement est strictement positif.")
        jour = _date(date_paiement)

        def _t(s: Session) -> dict:
            compte_dues = self._parametre(s, "compte_remunerations_dues")
            ecr = self._reglement(
                s, montant_=m, compte_tresorerie=compte, jour=jour,
                compte_tiers=compte_dues, tiers_ref=salarie_ref,
                tiers_nom=salarie_nom, encaissement=False,
                source_type="paie.reglement",
                source_id=_texte(source_id, champ="identifiant", max_len=64),
                document_lie=bulletin_source_id,
                libelle=f"Paiement salaire {salarie_nom or salarie_ref}",
                reference=None)
            code = None
            if bulletin_source_id:
                code = self._lettrer_document(s, compte_dues, salarie_ref,
                                              bulletin_source_id,
                                              ("paie", "paie.reglement"))
            return {"paiement": ecr, "lettrage": code}
        return self._executer_avec_reprise_numerotation(_t, session)

    def comptabiliser_avance_salarie(self, source_id: str, date_avance, montant_,
                                     *, salarie_ref: str,
                                     salarie_nom: str | None = None,
                                     compte: str = "5711",
                                     session: Session | None = None) -> dict:
        m = montant(montant_, champ="avance")
        if m <= ZERO:
            raise EcritureInvalide("Une avance est strictement positive.")
        jour = _date(date_avance)

        def _t(s: Session) -> dict:
            return self._reglement(
                s, montant_=m, compte_tresorerie=compte, jour=jour,
                compte_tiers=self._parametre(s, "compte_avances_personnel"),
                tiers_ref=salarie_ref, tiers_nom=salarie_nom, encaissement=False,
                source_type="avance_salarie",
                source_id=_texte(source_id, champ="identifiant", max_len=64),
                document_lie=None,
                libelle=f"Avance sur salaire {salarie_nom or salarie_ref}",
                reference=None)
        return self._executer_avec_reprise_numerotation(_t, session)

    # -- stocks ---------------------------------------------------------

    def comptabiliser_inventaire(self, source_id: str, date_inventaire, *,
                                 stock_initial, stock_final,
                                 compte_stock: str | None = None,
                                 compte_variation: str | None = None,
                                 session: Session | None = None) -> dict | None:
        """Variation de stock de fin de période (inventaire intermittent).

        Annule le stock initial (D 6031 / C 31x) et constate le stock final
        valorisé (D 31x / C 6031). L'ERP fournit les deux valeurs — par
        exemple ``stock.valeur_stock()`` au coût d'achat."""
        initial = montant(stock_initial, champ="stock initial")
        final = montant(stock_final, champ="stock final")
        if initial < ZERO or final < ZERO:
            raise EcritureInvalide("Une valeur de stock est positive ou nulle.")
        if initial == ZERO and final == ZERO:
            return None
        jour = _date(date_inventaire)

        def _t(s: Session) -> dict:
            stock = compte_stock or self._parametre(s, "compte_stock_marchandises")
            variation = compte_variation or self._parametre(
                s, "compte_variation_stock_marchandises")
            lignes = []
            if initial:
                lignes += [Ligne(compte=variation, debit=initial,
                                 libelle="Annulation du stock initial"),
                           Ligne(compte=stock, credit=initial,
                                 libelle="Annulation du stock initial")]
            if final:
                lignes += [Ligne(compte=stock, debit=final,
                                 libelle="Constatation du stock final"),
                           Ligne(compte=variation, credit=final,
                                 libelle="Constatation du stock final")]
            return self._saisir(self._parametre(s, "journal_od"), jour,
                                "Variation des stocks (inventaire)", lignes,
                                reference=None, valider=True,
                                source_type="inventaire",
                                source_id=_texte(source_id, champ="identifiant",
                                                 max_len=64),
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)
