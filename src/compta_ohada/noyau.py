"""
Noyau du grand livre : référentiels, saisie en partie double, validation,
numérotation, contre-passation, lettrage, verrouillage, contrôles.

Toutes les méthodes publiques acceptent un argument ``session`` optionnel :

- sans session, l'opération ouvre sa propre transaction, la valide, et la
  rejoue si SQLite répond « database is locked » (même mécanique que
  ``robustesse.executer_ecriture`` des ERP) ;
- avec la session de l'ERP, elle s'inscrit DANS la transaction de l'appelant
  (flush, sans commit) : la vente et son écriture comptable sont alors
  enregistrées ensemble ou pas du tout.

Les méthodes renvoient des dictionnaires (jamais d'objets ORM détachés).
"""

from __future__ import annotations

import logging
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from . import garde_fous  # noqa: F401  (enregistre le garde-fou ORM)
from .erreurs import (CompteInconnu, CompteNonImputable, DejaContrepassee,
                      EcritureDesequilibree, EcritureInvalide,
                      EcritureVerrouillee, ExerciceClos, ExerciceIntrouvable,
                      JournalInconnu, LettrageInvalide, ParametreInvalide,
                      PeriodeVerrouillee, TaxeInconnue)
from .integrite import GENESE, calculer_empreinte
from .modeles import (Compte, Ecriture, Exercice, Journal, Lettrage,
                      LigneEcriture, LigneLettrage, Parametre, Taxe,
                      id_deterministe)
from .plan_syscohada import (PLAN_SYSCOHADA, classe_de,
                             est_imputable_par_defaut, est_lettrable_par_defaut)
from .socle import (INCLURE_SUPPRIMES, ZERO, marquer_supprime, montant,
                    utc_now)

log = logging.getLogger("compta_ohada")

# ---------------------------------------------------------------------------
# Paramétrage par défaut d'un tenant
# ---------------------------------------------------------------------------

JOURNAUX_PAR_DEFAUT: tuple[tuple[str, str, str, str | None], ...] = (
    ("VT", "Ventes", Journal.VENTE, None),
    ("AC", "Achats", Journal.ACHAT, None),
    ("CA", "Caisse", Journal.TRESORERIE, "5711"),
    ("BQ", "Banque", Journal.TRESORERIE, "5211"),
    ("MM", "Monnaie électronique (mobile money)", Journal.TRESORERIE, "552"),
    ("OD", "Opérations diverses", Journal.OD, None),
    ("PA", "Paie", Journal.PAIE, None),
    ("AN", "À-nouveaux", Journal.A_NOUVEAU, None),
)

PARAMETRES_PAR_DEFAUT: dict[str, str] = {
    "devise": "XOF",
    "assujetti_tva": "1",
    "date_verrouillage": "",
    "compte_client": "4111",
    "compte_fournisseur": "4011",
    "compte_acompte_client": "4191",
    "compte_acompte_fournisseur": "4091",
    "compte_vente_marchandises": "7011",
    "compte_vente_services": "706",
    "compte_achat_marchandises": "6011",
    "compte_stock_marchandises": "311",
    "compte_variation_stock_marchandises": "6031",
    "compte_remise_accordee": "7019",
    "compte_remise_obtenue": "6019",
    "compte_salaires": "6611",
    "compte_charges_sociales": "6641",
    "compte_remunerations_dues": "422",
    "compte_avances_personnel": "421",
    "compte_tva_due": "4441",
    "compte_credit_tva": "4449",
    "compte_resultat_benefice": "131",
    "compte_resultat_perte": "139",
    "compte_report_crediteur": "121",
    "compte_report_debiteur": "129",
    "compte_ecart_caisse_perte": "658",
    "compte_ecart_caisse_gain": "758",
    "journal_ventes": "VT",
    "journal_achats": "AC",
    "journal_od": "OD",
    "journal_paie": "PA",
    "journal_a_nouveaux": "AN",
    # Devises (voir devises.py)
    "compte_perte_change": "656",
    "compte_gain_change": "756",
    "compte_perte_change_financiere": "676",
    "compte_gain_change_financiere": "776",
    "compte_ecart_conversion_actif": "478",
    "compte_ecart_conversion_passif": "479",
    "compte_provision_change": "194",
    "compte_dotation_provision_financiere": "697",
    "compte_reprise_provision_financiere": "797",
}

LONGUEUR_MAX_LIBELLE = 255

# Écritures de fin d'exercice : passées à la clôture, qui intervient en
# général APRÈS le verrouillage de la période (déclarations déposées).
TYPES_HORS_VERROU = (Ecriture.CLOTURE, Ecriture.A_NOUVEAU)


def _texte(valeur, *, champ: str, max_len: int, obligatoire: bool = True) -> str | None:
    """Nettoie une saisie : caractères de contrôle retirés, NFC, longueur bornée."""
    if valeur is None:
        if obligatoire:
            raise EcritureInvalide(f"{champ} obligatoire.")
        return None
    texte = unicodedata.normalize("NFC", str(valeur))
    texte = "".join(" " if c in "\t\n\r" else c for c in texte
                    if c in "\t\n\r" or unicodedata.category(c)[0] != "C")
    texte = " ".join(texte.split())
    if not texte:
        if obligatoire:
            raise EcritureInvalide(f"{champ} obligatoire.")
        return None
    if len(texte) > max_len:
        raise EcritureInvalide(
            f"{champ} trop long ({len(texte)} caractères, maximum {max_len}).")
    return texte


def _date(valeur, *, champ: str = "date") -> date:
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    if isinstance(valeur, str):
        try:
            return date.fromisoformat(valeur.strip())
        except ValueError:
            pass
    raise EcritureInvalide(f"{champ} invalide : {valeur!r} (attendu AAAA-MM-JJ).")


def valider_code_devise(code) -> str:
    code = str(code or "").strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise EcritureInvalide(f"Code devise invalide : {code!r} (ISO 4217, ex. EUR).")
    return code


def _numero_compte(numero) -> str:
    numero = str(numero or "").strip()
    if not numero.isdigit() or len(numero) > 20 or numero[0] == "0":
        raise CompteInconnu(f"Numéro de compte invalide : {numero!r}.")
    return numero


# ---------------------------------------------------------------------------
# Saisie
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ligne:
    """Ligne d'écriture à saisir. Montant au débit OU au crédit."""

    compte: str
    debit: Any = 0
    credit: Any = 0
    libelle: str | None = None
    tiers_ref: str | None = None
    tiers_nom: str | None = None
    date_echeance: date | None = None
    taxe_code: str | None = None
    base_taxe: Any = None
    devise: str | None = None
    montant_devise: Any = None
    section: str | None = None

    @classmethod
    def depuis(cls, valeur) -> "Ligne":
        if isinstance(valeur, Ligne):
            return valeur
        if isinstance(valeur, dict):
            inconnues = set(valeur) - set(cls.__dataclass_fields__)
            if inconnues:
                raise EcritureInvalide(
                    f"Champ(s) de ligne inconnu(s) : {', '.join(sorted(inconnues))}.")
            return cls(**valeur)
        raise EcritureInvalide(f"Ligne d'écriture illisible : {valeur!r}.")


def resume_ecriture(e: Ecriture, lignes: Iterable[LigneEcriture]) -> dict:
    return {
        "id": e.id,
        "numero": e.numero,
        "journal": e.journal_code,
        "date": e.date_ecriture,
        "libelle": e.libelle,
        "reference": e.reference,
        "statut": e.statut,
        "type": e.type_ecriture,
        "source_type": e.source_type,
        "source_id": e.source_id,
        "contrepasse_id": e.contrepasse_id,
        "document_lie": e.document_lie,
        "total": e.total,
        "poste": e.poste,
        "empreinte": e.empreinte,
        "lignes": [{
            "id": lg.id, "ordre": lg.ordre, "compte": lg.compte_numero,
            "libelle": lg.libelle, "debit": lg.debit, "credit": lg.credit,
            "tiers_ref": lg.tiers_ref, "tiers_nom": lg.tiers_nom,
            "date_echeance": lg.date_echeance, "taxe_code": lg.taxe_code,
            "base_taxe": lg.base_taxe, "devise": lg.devise,
            "montant_devise": lg.montant_devise, "section": lg.section,
        } for lg in sorted(lignes, key=lambda x: x.ordre)],
    }


class Noyau:
    """Grand livre d'UN tenant, vu depuis UN poste."""

    def __init__(self, engine, tenant_id: str, *, poste: str = "P1",
                 utilisateur: str | None = None, tentatives: int = 5):
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id or len(tenant_id) > 36:
            raise ParametreInvalide("tenant_id obligatoire (36 caractères max).")
        poste = str(poste or "").strip().upper()
        if not poste or len(poste) > 20 or not all(
                c.isalnum() or c in "-_" for c in poste):
            raise ParametreInvalide(
                "Code poste invalide : lettres, chiffres, - et _ (20 max).")
        self.engine = engine
        self.tenant_id = tenant_id
        self.poste = poste
        self.utilisateur = utilisateur
        self._tentatives = tentatives

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def _executer(self, travail: Callable[[Session], Any],
                  session: Session | None = None):
        if session is not None:
            resultat = travail(session)
            session.flush()
            return resultat
        derniere = None
        for essai in range(self._tentatives):
            try:
                with Session(self.engine, expire_on_commit=False) as s:
                    resultat = travail(s)
                    s.commit()
                    return resultat
            except OperationalError as exc:
                derniere = exc
                message = str(exc).lower()
                if "locked" not in message or essai == self._tentatives - 1:
                    raise
                delai = 0.1 * (2 ** essai) + (essai % 7) / 100.0
                log.warning("Verrou base (essai %d) — reprise dans %.2fs.",
                            essai + 1, delai)
                time.sleep(delai)
        raise derniere  # pragma: no cover

    def _lire(self, travail: Callable[[Session], Any],
              session: Session | None = None):
        if session is not None:
            return travail(session)
        with Session(self.engine) as s:
            return travail(s)

    # ------------------------------------------------------------------
    # Initialisation (idempotente)
    # ------------------------------------------------------------------

    def initialiser(self, *, exercice: int | None = None,
                    taux_tva: Any = "18", devise: str = "XOF",
                    assujetti_tva: bool = True,
                    session: Session | None = None) -> dict:
        """Installe le plan SYSCOHADA, les journaux, les taxes, les paramètres
        et (optionnellement) l'exercice de l'année donnée.

        Idempotent et sûr hors-ligne : les identifiants sont déterministes,
        deux postes qui initialisent le même tenant produisent les mêmes
        lignes. Ne modifie jamais un élément déjà présent (un libellé
        personnalisé par le tenant est conservé)."""
        taux = montant(taux_tva, champ="taux de TVA")
        if taux < 0 or taux >= 100:
            raise ParametreInvalide("Taux de TVA hors bornes (0 à 99,99 %).")
        devise = _texte(devise, champ="devise", max_len=3).upper()

        def _t(s: Session) -> dict:
            crees = {"comptes": 0, "journaux": 0, "taxes": 0, "parametres": 0}
            existants = set(s.scalars(
                select(Compte.numero).where(Compte.tenant_id == self.tenant_id)
                .execution_options(**INCLURE_SUPPRIMES)))
            for numero, libelle in PLAN_SYSCOHADA:
                if numero in existants:
                    continue
                s.add(Compte(
                    id=id_deterministe(self.tenant_id, "compte", numero),
                    tenant_id=self.tenant_id, numero=numero, libelle=libelle,
                    classe=classe_de(numero),
                    imputable=est_imputable_par_defaut(numero),
                    lettrable=est_lettrable_par_defaut(numero),
                    standard=True))
                crees["comptes"] += 1
            s.flush()

            codes_j = set(s.scalars(
                select(Journal.code).where(Journal.tenant_id == self.tenant_id)
                .execution_options(**INCLURE_SUPPRIMES)))
            for code, libelle, type_j, compte_t in JOURNAUX_PAR_DEFAUT:
                if code in codes_j:
                    continue
                s.add(Journal(
                    id=id_deterministe(self.tenant_id, "journal", code),
                    tenant_id=self.tenant_id, code=code, libelle=libelle,
                    type_journal=type_j, compte_tresorerie=compte_t))
                crees["journaux"] += 1

            pb = int(taux * 100)
            taxes = (
                (f"TVA{_code_taux(taux)}", f"TVA collectée {taux} %", pb,
                 Taxe.VENTE, "4431"),
                (f"TVA{_code_taux(taux)}-SRV",
                 f"TVA collectée sur prestations {taux} %", pb, Taxe.VENTE,
                 "4432"),
                (f"TVA{_code_taux(taux)}-ACH", f"TVA déductible sur achats {taux} %",
                 pb, Taxe.ACHAT, "4452"),
                (f"TVA{_code_taux(taux)}-FG",
                 f"TVA déductible sur services et frais {taux} %", pb,
                 Taxe.ACHAT, "4454"),
                (f"TVA{_code_taux(taux)}-IMMO",
                 f"TVA déductible sur immobilisations {taux} %", pb,
                 Taxe.ACHAT, "4451"),
                ("EXO", "Exonéré de TVA (vente)", 0, Taxe.VENTE, "4431"),
                ("EXO-ACH", "Exonéré de TVA (achat)", 0, Taxe.ACHAT, "4452"),
            )
            codes_t = set(s.scalars(
                select(Taxe.code).where(Taxe.tenant_id == self.tenant_id)
                .execution_options(**INCLURE_SUPPRIMES)))
            for code, libelle, taux_pb, type_t, compte in taxes:
                if code in codes_t:
                    continue
                s.add(Taxe(id=id_deterministe(self.tenant_id, "taxe", code),
                           tenant_id=self.tenant_id, code=code, libelle=libelle,
                           taux_pb=taux_pb, type_taxe=type_t, compte=compte))
                crees["taxes"] += 1

            valeurs = dict(PARAMETRES_PAR_DEFAUT)
            valeurs["devise"] = devise
            valeurs["assujetti_tva"] = "1" if assujetti_tva else "0"
            valeurs["taxe_vente_defaut"] = (
                f"TVA{_code_taux(taux)}" if assujetti_tva else "EXO")
            valeurs["taxe_achat_defaut"] = (
                f"TVA{_code_taux(taux)}-ACH" if assujetti_tva else "EXO-ACH")
            cles = set(s.scalars(
                select(Parametre.cle).where(Parametre.tenant_id == self.tenant_id)
                .execution_options(**INCLURE_SUPPRIMES)))
            for cle, valeur in valeurs.items():
                if cle in cles:
                    continue
                s.add(Parametre(
                    id=id_deterministe(self.tenant_id, "parametre", cle),
                    tenant_id=self.tenant_id, cle=cle, valeur=valeur))
                crees["parametres"] += 1
            s.flush()
            # Parité fixe du franc CFA avec l'euro (1 EUR = 655,957 XOF/XAF).
            if devise in ("XOF", "XAF"):
                from .modeles import Cours
                cid = id_deterministe(self.tenant_id, "cours", "EUR|1999-01-01")
                if s.get(Cours, cid, execution_options=INCLURE_SUPPRIMES) is None:
                    s.add(Cours(id=cid, tenant_id=self.tenant_id, devise="EUR",
                                date_cours=date(1999, 1, 1),
                                cours_micro=655_957_000))
                    crees["cours"] = 1
            if exercice is not None:
                code = str(exercice)
                if self._exercice_par_code(s, code) is None:
                    self._creer_exercice(s, code, date(int(exercice), 1, 1),
                                         date(int(exercice), 12, 31))
                    crees["exercice"] = code
            return crees

        return self._executer(_t, session)

    # ------------------------------------------------------------------
    # Paramètres
    # ------------------------------------------------------------------

    def _parametre(self, s: Session, cle: str, defaut: str | None = None) -> str | None:
        p = s.scalar(select(Parametre).where(
            Parametre.tenant_id == self.tenant_id, Parametre.cle == cle))
        if p is None:
            return PARAMETRES_PAR_DEFAUT.get(cle, defaut)
        return p.valeur

    def parametre(self, cle: str, *, session: Session | None = None) -> str | None:
        return self._lire(lambda s: self._parametre(s, cle), session)

    def definir_parametre(self, cle: str, valeur, *,
                          session: Session | None = None) -> None:
        cle = _texte(cle, champ="clé", max_len=60)
        if cle == "date_verrouillage":
            raise ParametreInvalide(
                "Utilisez verrouiller_jusqu_au() pour la date de verrouillage.")
        valeur = None if valeur is None else _texte(
            valeur, champ="valeur", max_len=255, obligatoire=False)
        if valeur and (cle.startswith("compte_") or cle.startswith("journal_")
                       or cle.startswith("taxe_")):

            def _verif(s):
                if cle.startswith("compte_"):
                    self._compte(s, valeur, pour_imputation=True)
                elif cle.startswith("journal_"):
                    self._journal(s, valeur)
                else:
                    self._taxe(s, valeur)
            self._lire(_verif, session)

        def _t(s: Session) -> None:
            self._ecrire_parametre(s, cle, valeur)
        self._executer(_t, session)

    def _ecrire_parametre(self, s: Session, cle: str, valeur) -> None:
        p = s.get(Parametre, id_deterministe(self.tenant_id, "parametre", cle),
                  execution_options=INCLURE_SUPPRIMES)
        if p is None:
            s.add(Parametre(id=id_deterministe(self.tenant_id, "parametre", cle),
                            tenant_id=self.tenant_id, cle=cle, valeur=valeur))
        else:
            p.valeur = valeur
            p.deleted_at = None
            p.is_synced = False

    # ------------------------------------------------------------------
    # Comptes
    # ------------------------------------------------------------------

    def _compte(self, s: Session, numero: str, *, pour_imputation: bool = False) -> Compte:
        numero = _numero_compte(numero)
        compte = s.scalar(select(Compte).where(
            Compte.tenant_id == self.tenant_id, Compte.numero == numero))
        if compte is None or not compte.actif:
            raise CompteInconnu(
                f"Compte {numero} inconnu ou désactivé dans le plan comptable.")
        if pour_imputation and not compte.imputable:
            raise CompteNonImputable(
                f"Le compte {numero} ({compte.libelle}) est un compte de "
                "regroupement : utilisez un compte de détail (3 chiffres ou plus).")
        return compte

    def creer_compte(self, numero: str, libelle: str, *,
                     lettrable: bool | None = None, devise: str | None = None,
                     session: Session | None = None) -> dict:
        """Ajoute un compte au plan du tenant (ex. 5521 « Orange Money »).

        Le numéro doit prolonger une rubrique existante (ses deux premiers
        chiffres) : on n'invente pas de classe ni de rubrique SYSCOHADA."""
        numero = _numero_compte(numero)
        if len(numero) < 3:
            raise CompteNonImputable(
                "Les classes et rubriques sont fixées par le SYSCOHADA : "
                "créez un compte d'au moins 3 chiffres.")
        libelle = _texte(libelle, champ="libellé", max_len=200)
        devise = valider_code_devise(devise) if devise else None

        def _t(s: Session) -> dict:
            if devise and devise == (self._parametre(s, "devise") or "XOF"):
                raise CompteInconnu(f"{devise} est la monnaie de tenue.")
            rubrique = s.scalar(select(Compte).where(
                Compte.tenant_id == self.tenant_id, Compte.numero == numero[:2]))
            if rubrique is None:
                raise CompteInconnu(
                    f"Rubrique {numero[:2]} absente du plan : initialisez le "
                    "tenant avant de créer des comptes.")
            cid = id_deterministe(self.tenant_id, "compte", numero)
            compte = s.get(Compte, cid, execution_options=INCLURE_SUPPRIMES)
            if compte is None:
                compte = s.scalar(select(Compte).where(
                    Compte.tenant_id == self.tenant_id, Compte.numero == numero)
                    .execution_options(**INCLURE_SUPPRIMES))
            if compte is not None and compte.deleted_at is None and compte.actif:
                raise CompteInconnu(f"Le compte {numero} existe déjà.")
            if compte is None:
                compte = Compte(id=cid, tenant_id=self.tenant_id, numero=numero,
                                classe=classe_de(numero), imputable=True,
                                standard=False, libelle=libelle)
                s.add(compte)
            compte.libelle = libelle
            compte.actif = True
            compte.deleted_at = None
            compte.is_synced = False
            compte.lettrable = (est_lettrable_par_defaut(numero)
                                if lettrable is None else bool(lettrable))
            compte.devise = devise
            s.flush()
            return self._compte_dict(compte)

        return self._executer(_t, session)

    def modifier_compte(self, numero: str, *, libelle: str | None = None,
                        lettrable: bool | None = None,
                        session: Session | None = None) -> dict:
        def _t(s: Session) -> dict:
            compte = self._compte(s, numero)
            if libelle is not None:
                compte.libelle = _texte(libelle, champ="libellé", max_len=200)
            if lettrable is not None:
                compte.lettrable = bool(lettrable)
            compte.is_synced = False
            return self._compte_dict(compte)
        return self._executer(_t, session)

    def desactiver_compte(self, numero: str, *,
                          session: Session | None = None) -> None:
        """Retire un compte de la saisie. Refusé s'il porte un solde : un
        compte qui « disparaît » avec de l'argent dessus fausse le bilan."""
        def _t(s: Session) -> None:
            compte = self._compte(s, numero)
            solde = s.scalar(select(
                func.coalesce(func.sum(LigneEcriture.debit), 0)
                - func.coalesce(func.sum(LigneEcriture.credit), 0))
                .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                .where(LigneEcriture.tenant_id == self.tenant_id,
                       LigneEcriture.compte_numero == compte.numero,
                       Ecriture.statut == Ecriture.VALIDEE))
            if solde:
                raise CompteInconnu(
                    f"Le compte {compte.numero} a un solde non nul : "
                    "soldez-le avant de le désactiver.")
            compte.actif = False
            compte.is_synced = False
        self._executer(_t, session)

    @staticmethod
    def _compte_dict(c: Compte) -> dict:
        return {"numero": c.numero, "libelle": c.libelle, "classe": c.classe,
                "imputable": c.imputable, "lettrable": c.lettrable,
                "actif": c.actif, "standard": c.standard, "devise": c.devise}

    def comptes(self, *, classe: int | None = None, prefixe: str | None = None,
                inclure_inactifs: bool = False,
                session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            req = select(Compte).where(Compte.tenant_id == self.tenant_id)
            if classe is not None:
                req = req.where(Compte.classe == int(classe))
            if prefixe:
                req = req.where(Compte.numero.startswith(str(prefixe)))
            if not inclure_inactifs:
                req = req.where(Compte.actif.is_(True))
            return [self._compte_dict(c) for c in s.scalars(req)]
        resultat = self._lire(_t, session)
        return sorted(resultat, key=lambda c: c["numero"])

    # ------------------------------------------------------------------
    # Journaux
    # ------------------------------------------------------------------

    def _journal(self, s: Session, code: str) -> Journal:
        code = str(code or "").strip().upper()
        j = s.scalar(select(Journal).where(
            Journal.tenant_id == self.tenant_id, Journal.code == code))
        if j is None or not j.actif:
            raise JournalInconnu(f"Journal {code!r} inconnu ou désactivé.")
        return j

    def creer_journal(self, code: str, libelle: str, type_journal: str, *,
                      compte_tresorerie: str | None = None,
                      session: Session | None = None) -> dict:
        code = _texte(code, champ="code journal", max_len=8).upper()
        if not code.isalnum():
            raise JournalInconnu("Code journal : lettres et chiffres uniquement.")
        libelle = _texte(libelle, champ="libellé", max_len=120)
        if type_journal not in Journal.TYPES:
            raise JournalInconnu(
                f"Type de journal inconnu : {type_journal!r} "
                f"(attendu : {', '.join(Journal.TYPES)}).")
        if type_journal == Journal.TRESORERIE and not compte_tresorerie:
            raise JournalInconnu(
                "Un journal de trésorerie doit désigner son compte (classe 5).")

        def _t(s: Session) -> dict:
            if compte_tresorerie:
                c = self._compte(s, compte_tresorerie, pour_imputation=True)
                if c.classe != 5:
                    raise JournalInconnu(
                        "Le compte d'un journal de trésorerie est de classe 5.")
            jid = id_deterministe(self.tenant_id, "journal", code)
            j = s.get(Journal, jid, execution_options=INCLURE_SUPPRIMES)
            if j is not None and j.deleted_at is None and j.actif:
                raise JournalInconnu(f"Le journal {code} existe déjà.")
            if j is None:
                j = Journal(id=jid, tenant_id=self.tenant_id, code=code,
                            libelle=libelle, type_journal=type_journal)
                s.add(j)
            j.libelle = libelle
            j.type_journal = type_journal
            j.compte_tresorerie = compte_tresorerie
            j.actif = True
            j.deleted_at = None
            j.is_synced = False
            return self._journal_dict(j)
        return self._executer(_t, session)

    @staticmethod
    def _journal_dict(j: Journal) -> dict:
        return {"code": j.code, "libelle": j.libelle, "type": j.type_journal,
                "compte_tresorerie": j.compte_tresorerie, "actif": j.actif}

    def journaux(self, *, session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            return [self._journal_dict(j) for j in s.scalars(
                select(Journal).where(Journal.tenant_id == self.tenant_id)
                .order_by(Journal.code))]
        return self._lire(_t, session)

    def journal_tresorerie_pour(self, compte: str, *,
                                session: Session | None = None) -> str:
        """Code du journal de trésorerie qui mouvemente ce compte."""
        def _t(s: Session) -> str:
            for j in s.scalars(select(Journal).where(
                    Journal.tenant_id == self.tenant_id,
                    Journal.type_journal == Journal.TRESORERIE,
                    Journal.actif.is_(True)).order_by(Journal.code)):
                if j.compte_tresorerie == str(compte):
                    return j.code
            raise JournalInconnu(
                f"Aucun journal de trésorerie pour le compte {compte} : "
                "créez-le avec creer_journal(..., compte_tresorerie=...).")
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Taxes
    # ------------------------------------------------------------------

    def _taxe(self, s: Session, code: str) -> Taxe:
        code = str(code or "").strip().upper()
        t = s.scalar(select(Taxe).where(Taxe.tenant_id == self.tenant_id,
                                        Taxe.code == code))
        if t is None or not t.actif:
            raise TaxeInconnue(f"Taxe {code!r} inconnue ou désactivée.")
        return t

    def creer_taxe(self, code: str, libelle: str, taux, type_taxe: str,
                   compte: str, *, session: Session | None = None) -> dict:
        code = _texte(code, champ="code taxe", max_len=20).upper()
        libelle = _texte(libelle, champ="libellé", max_len=120)
        taux = montant(taux, champ="taux")
        if taux < 0 or taux >= 100:
            raise TaxeInconnue("Taux hors bornes (0 à 99,99 %).")
        if type_taxe not in (Taxe.VENTE, Taxe.ACHAT):
            raise TaxeInconnue("Type de taxe : VENTE ou ACHAT.")

        def _t(s: Session) -> dict:
            c = self._compte(s, compte, pour_imputation=True)
            if not c.numero.startswith("44"):
                raise TaxeInconnue("Le compte d'une taxe est un compte 44x (État).")
            tid = id_deterministe(self.tenant_id, "taxe", code)
            t = s.get(Taxe, tid, execution_options=INCLURE_SUPPRIMES)
            if t is not None and t.deleted_at is None and t.actif:
                raise TaxeInconnue(f"La taxe {code} existe déjà.")
            if t is None:
                t = Taxe(id=tid, tenant_id=self.tenant_id, code=code,
                         libelle=libelle, taux_pb=0, type_taxe=type_taxe,
                         compte=c.numero)
                s.add(t)
            t.libelle = libelle
            t.taux_pb = int(taux * 100)
            t.type_taxe = type_taxe
            t.compte = c.numero
            t.actif = True
            t.deleted_at = None
            t.is_synced = False
            return self._taxe_dict(t)
        return self._executer(_t, session)

    @staticmethod
    def _taxe_dict(t: Taxe) -> dict:
        return {"code": t.code, "libelle": t.libelle, "taux": t.taux,
                "type": t.type_taxe, "compte": t.compte, "actif": t.actif}

    def taxes(self, *, session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            return [self._taxe_dict(t) for t in s.scalars(
                select(Taxe).where(Taxe.tenant_id == self.tenant_id)
                .order_by(Taxe.code))]
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Exercices
    # ------------------------------------------------------------------

    def _exercice_par_code(self, s: Session, code: str) -> Exercice | None:
        return s.scalar(select(Exercice).where(
            Exercice.tenant_id == self.tenant_id, Exercice.code == code))

    def _exercice_pour(self, s: Session, jour: date) -> Exercice:
        ex = s.scalar(select(Exercice).where(
            Exercice.tenant_id == self.tenant_id,
            Exercice.date_debut <= jour, Exercice.date_fin >= jour))
        if ex is None:
            raise ExerciceIntrouvable(
                f"Aucun exercice comptable ne couvre le {jour:%d/%m/%Y} : "
                "ouvrez l'exercice correspondant.")
        return ex

    def _creer_exercice(self, s: Session, code: str, debut: date, fin: date) -> Exercice:
        if fin <= debut:
            raise ExerciceIntrouvable("La fin de l'exercice doit suivre son début.")
        if (fin - debut).days > 731:
            raise ExerciceIntrouvable("Un exercice ne dépasse pas 24 mois.")
        chevauche = s.scalar(select(Exercice).where(
            Exercice.tenant_id == self.tenant_id,
            Exercice.date_debut <= fin, Exercice.date_fin >= debut))
        if chevauche is not None:
            raise ExerciceIntrouvable(
                f"L'exercice chevauche l'exercice {chevauche.code}.")
        eid = id_deterministe(self.tenant_id, "exercice", code)
        ex = s.get(Exercice, eid, execution_options=INCLURE_SUPPRIMES)
        if ex is not None:
            raise ExerciceIntrouvable(f"L'exercice {code} existe déjà.")
        ex = Exercice(id=eid, tenant_id=self.tenant_id, code=code,
                      date_debut=debut, date_fin=fin, statut=Exercice.OUVERT)
        s.add(ex)
        s.flush()
        return ex

    def ouvrir_exercice(self, code, debut, fin, *,
                        session: Session | None = None) -> dict:
        code = _texte(code, champ="code exercice", max_len=20)
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> dict:
            return self._exercice_dict(self._creer_exercice(s, code, debut, fin))
        return self._executer(_t, session)

    @staticmethod
    def _exercice_dict(e: Exercice) -> dict:
        return {"code": e.code, "debut": e.date_debut, "fin": e.date_fin,
                "statut": e.statut, "cloture_le": e.cloture_le}

    def exercices(self, *, session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            return [self._exercice_dict(e) for e in s.scalars(
                select(Exercice).where(Exercice.tenant_id == self.tenant_id)
                .order_by(Exercice.date_debut))]
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Verrouillage de période
    # ------------------------------------------------------------------

    def _date_verrouillage(self, s: Session) -> date | None:
        valeur = self._parametre(s, "date_verrouillage")
        return date.fromisoformat(valeur) if valeur else None

    def date_verrouillage(self, *, session: Session | None = None) -> date | None:
        return self._lire(self._date_verrouillage, session)

    def verrouiller_jusqu_au(self, jour, *, forcer: bool = False,
                             session: Session | None = None) -> None:
        """Interdit toute écriture datée de ``jour`` ou avant (période close :
        TVA déclarée, mois arrêté). Ne recule pas sans ``forcer=True``."""
        jour = _date(jour)

        def _t(s: Session) -> None:
            actuelle = self._date_verrouillage(s)
            if actuelle and jour < actuelle and not forcer:
                raise PeriodeVerrouillee(
                    f"La période est déjà verrouillée jusqu'au "
                    f"{actuelle:%d/%m/%Y} : on ne déverrouille pas sans "
                    "autorisation explicite (forcer=True).")
            brouillards = s.scalar(select(func.count(Ecriture.id)).where(
                Ecriture.tenant_id == self.tenant_id,
                Ecriture.statut == Ecriture.BROUILLARD,
                Ecriture.date_ecriture <= jour))
            if brouillards:
                raise PeriodeVerrouillee(
                    f"{brouillards} écriture(s) en brouillard dans la période : "
                    "validez-les ou supprimez-les avant de verrouiller.")
            self._ecrire_parametre(s, "date_verrouillage", jour.isoformat())
        self._executer(_t, session)

    def _controler_date(self, s: Session, jour: date, *,
                        ignorer_verrou: bool = False) -> Exercice:
        verrou = self._date_verrouillage(s)
        if verrou and jour <= verrou and not ignorer_verrou:
            raise PeriodeVerrouillee(
                f"Période verrouillée jusqu'au {verrou:%d/%m/%Y} : aucune "
                f"écriture ne peut être passée au {jour:%d/%m/%Y}.")
        ex = self._exercice_pour(s, jour)
        if ex.statut == Exercice.CLOTURE:
            raise ExerciceClos(f"L'exercice {ex.code} est clôturé.")
        return ex

    # ------------------------------------------------------------------
    # Saisie et validation
    # ------------------------------------------------------------------

    def _preparer_lignes(self, s: Session, lignes, libelle_defaut: str
                         ) -> tuple[list[tuple[Ligne, Compte, Decimal, Decimal]], Decimal]:
        lignes = [Ligne.depuis(lg) for lg in (lignes or [])]
        if len(lignes) < 2:
            raise EcritureInvalide("Une écriture compte au moins deux lignes.")
        if len(lignes) > 999:
            raise EcritureInvalide("Une écriture compte au plus 999 lignes.")
        preparees = []
        total_d = total_c = ZERO
        classe9_d = classe9_c = ZERO
        for i, lg in enumerate(lignes, 1):
            d = montant(lg.debit or 0, champ=f"ligne {i} débit")
            c = montant(lg.credit or 0, champ=f"ligne {i} crédit")
            if d < 0 or c < 0:
                raise EcritureInvalide(
                    f"Ligne {i} : montant négatif. Inversez le sens "
                    "(débit <-> crédit) au lieu de saisir un négatif.")
            if d and c:
                raise EcritureInvalide(
                    f"Ligne {i} : un montant au débit OU au crédit, pas les deux.")
            if not d and not c:
                raise EcritureInvalide(f"Ligne {i} : montant nul.")
            compte = self._compte(s, lg.compte, pour_imputation=True)
            self._controler_devise_ligne(s, i, lg, compte)
            if lg.section:
                self._controler_section(s, i, lg.section, compte)
            if lg.taxe_code:
                self._taxe(s, lg.taxe_code)
            if lg.base_taxe is not None:
                if montant(lg.base_taxe, champ=f"ligne {i} base") < 0:
                    raise EcritureInvalide(f"Ligne {i} : base de taxe négative.")
            total_d += d
            total_c += c
            if compte.classe == 9:
                classe9_d += d
                classe9_c += c
            preparees.append((lg, compte, d, c))
        if total_d != total_c:
            raise EcritureDesequilibree(
                f"Écriture déséquilibrée : débit {total_d} ≠ crédit {total_c} "
                f"(écart {total_d - total_c}).")
        if classe9_d != classe9_c:
            raise EcritureDesequilibree(
                "Les comptes de classe 9 (hors bilan / analytique) doivent "
                "s'équilibrer entre eux, sans contrepartie au bilan.")
        return preparees, total_d

    def _controler_devise_ligne(self, s: Session, i: int, lg: "Ligne",
                                compte: Compte) -> None:
        if lg.devise is None:
            if lg.montant_devise is not None:
                raise EcritureInvalide(f"Ligne {i} : montant en devise sans devise.")
            if compte.devise:
                raise EcritureInvalide(
                    f"Ligne {i} : le compte {compte.numero} est tenu en "
                    f"{compte.devise}, précisez le montant en devise.")
            return
        code = valider_code_devise(lg.devise)
        if code == (self._parametre(s, "devise") or "XOF"):
            raise EcritureInvalide(
                f"Ligne {i} : {code} est la monnaie de tenue, pas une devise.")
        if compte.devise and compte.devise != code:
            raise EcritureInvalide(
                f"Ligne {i} : le compte {compte.numero} est tenu en "
                f"{compte.devise}, pas en {code}.")
        if lg.montant_devise is None or montant(
                lg.montant_devise, champ=f"ligne {i} montant devise") < 0:
            raise EcritureInvalide(
                f"Ligne {i} : montant en devise manquant ou négatif.")

    def _controler_section(self, s: Session, i: int, code, compte: Compte) -> None:
        from .modeles import Section
        if compte.classe not in (6, 7, 8):
            raise EcritureInvalide(
                f"Ligne {i} : une section analytique ne se porte que sur un "
                "compte de charges ou de produits (classes 6, 7, 8).")
        code = str(code).strip().upper()
        section = s.scalar(select(Section).where(
            Section.tenant_id == self.tenant_id, Section.code == code))
        if section is None or not section.actif:
            raise EcritureInvalide(
                f"Ligne {i} : section analytique {code!r} inconnue ou désactivée.")

    def _ajouter_lignes(self, s: Session, ecriture: Ecriture, preparees,
                        libelle_defaut: str) -> list[LigneEcriture]:
        crees = []
        for ordre, (lg, compte, d, c) in enumerate(preparees, 1):
            ligne = LigneEcriture(
                tenant_id=self.tenant_id, ecriture_id=ecriture.id, ordre=ordre,
                compte_id=compte.id, compte_numero=compte.numero,
                libelle=_texte(lg.libelle, champ="libellé de ligne",
                               max_len=LONGUEUR_MAX_LIBELLE, obligatoire=False)
                or libelle_defaut,
                debit=d, credit=c,
                tiers_ref=_texte(lg.tiers_ref, champ="tiers", max_len=64,
                                 obligatoire=False),
                tiers_nom=_texte(lg.tiers_nom, champ="nom du tiers",
                                 max_len=200, obligatoire=False),
                date_echeance=(_date(lg.date_echeance, champ="échéance")
                               if lg.date_echeance else None),
                taxe_code=(str(lg.taxe_code).strip().upper()
                           if lg.taxe_code else None),
                base_taxe=(montant(lg.base_taxe) if lg.base_taxe is not None
                           else None),
                devise=valider_code_devise(lg.devise) if lg.devise else None,
                montant_devise=(montant(lg.montant_devise) if lg.devise else None),
                section=str(lg.section).strip().upper() if lg.section else None)
            s.add(ligne)
            crees.append(ligne)
        return crees

    def _ecriture_source(self, s: Session, source_type: str, source_id: str
                         ) -> Ecriture | None:
        """Écriture VIVANTE (non contre-passée) d'un document source."""
        candidates = list(s.scalars(select(Ecriture).where(
            Ecriture.tenant_id == self.tenant_id,
            Ecriture.source_type == source_type,
            Ecriture.source_id == source_id,
            Ecriture.type_ecriture != Ecriture.CONTREPASSATION)))
        for e in candidates:
            if not self._est_contrepassee(s, e.id):
                return e
        return None

    def _est_contrepassee(self, s: Session, ecriture_id: str) -> bool:
        return s.scalar(select(func.count(Ecriture.id)).where(
            Ecriture.tenant_id == self.tenant_id,
            Ecriture.contrepasse_id == ecriture_id)) > 0

    def saisir(self, journal: str, date_ecriture, libelle: str, lignes, *,
               reference: str | None = None, valider: bool = True,
               source_type: str | None = None, source_id: str | None = None,
               document_lie: str | None = None,
               session: Session | None = None) -> dict:
        """Saisit une écriture (validée par défaut).

        Avec ``source_type``/``source_id`` l'appel est IDEMPOTENT : rejouer la
        même saisie (reprise après coupure, double clic, resynchronisation)
        renvoie l'écriture existante au lieu d'en créer une seconde."""
        return self._saisir(journal, date_ecriture, libelle, lignes,
                            reference=reference, valider=valider,
                            source_type=source_type, source_id=source_id,
                            document_lie=document_lie,
                            type_ecriture=Ecriture.NORMALE, session=session)

    def _saisir(self, journal, date_ecriture, libelle, lignes, *, reference,
                valider, source_type, source_id, type_ecriture,
                document_lie: str | None = None,
                ignorer_verrou: bool = False,
                contrepasse_id: str | None = None,
                session: Session | None = None) -> dict:
        jour = _date(date_ecriture)
        libelle = _texte(libelle, champ="libellé", max_len=LONGUEUR_MAX_LIBELLE)
        reference = _texte(reference, champ="référence", max_len=60,
                           obligatoire=False)
        source_type = _texte(source_type, champ="type de source", max_len=40,
                             obligatoire=False)
        source_id = _texte(source_id, champ="identifiant de source", max_len=64,
                           obligatoire=False)
        document_lie = _texte(document_lie, champ="document lié", max_len=64,
                              obligatoire=False)
        if bool(source_type) != bool(source_id):
            raise EcritureInvalide(
                "source_type et source_id vont ensemble (ou aucun des deux).")

        def _t(s: Session) -> dict:
            if source_type:
                existante = self._ecriture_source(s, source_type, source_id)
                if existante is not None:
                    return resume_ecriture(existante, existante.lignes)
            j = self._journal(s, journal)
            if j.type_journal == Journal.A_NOUVEAU and type_ecriture != Ecriture.A_NOUVEAU:
                raise EcritureInvalide(
                    "Le journal des à-nouveaux est réservé à la réouverture "
                    "d'exercice.")
            if type_ecriture == Ecriture.A_NOUVEAU and j.type_journal != Journal.A_NOUVEAU:
                raise EcritureInvalide("Les à-nouveaux vont au journal AN.")
            hors_verrou = ignorer_verrou or type_ecriture in TYPES_HORS_VERROU
            ex = self._controler_date(s, jour, ignorer_verrou=hors_verrou)
            preparees, total = self._preparer_lignes(s, lignes, libelle)
            e = Ecriture(tenant_id=self.tenant_id, journal_id=j.id,
                         journal_code=j.code, exercice_id=ex.id,
                         date_ecriture=jour, libelle=libelle,
                         reference=reference, statut=Ecriture.BROUILLARD,
                         type_ecriture=type_ecriture, source_type=source_type,
                         source_id=source_id, contrepasse_id=contrepasse_id,
                         document_lie=document_lie,
                         total=total, poste=self.poste,
                         cree_par=self.utilisateur)
            s.add(e)
            s.flush()
            crees = self._ajouter_lignes(s, e, preparees, libelle)
            s.flush()
            if valider:
                self._valider(s, e, crees, ignorer_verrou=hors_verrou)
            return resume_ecriture(e, crees)

        return self._executer(_t, session)

    def _valider(self, s: Session, e: Ecriture, lignes: list[LigneEcriture], *,
                 ignorer_verrou: bool = False) -> None:
        if e.statut == Ecriture.VALIDEE:
            return
        ex = self._controler_date(
            s, e.date_ecriture,
            ignorer_verrou=ignorer_verrou or e.type_ecriture in TYPES_HORS_VERROU)
        if ex.id != e.exercice_id:
            e.exercice_id = ex.id
        total_d = sum((lg.debit for lg in lignes), ZERO)
        total_c = sum((lg.credit for lg in lignes), ZERO)
        if total_d != total_c or not total_d or len(lignes) < 2:
            raise EcritureDesequilibree("Écriture déséquilibrée : validation refusée.")
        # Sérialise la numérotation du journal : sur PostgreSQL, deux
        # validations simultanées liraient le même « dernier numéro » et
        # fourcheraient la chaîne d'empreintes. Le verrou de ligne (FOR
        # UPDATE, sans effet sur SQLite qui sérialise déjà les écrivains)
        # fait attendre la seconde jusqu'au commit de la première.
        # FOR NO KEY UPDATE (key_share=True) : compatible avec le verrou
        # KEY SHARE que PostgreSQL pose sur la ligne du journal à chaque
        # insertion d'écriture (clé étrangère) — un FOR UPDATE simple
        # s'interbloquerait avec lui.
        s.execute(select(Journal.id).where(Journal.id == e.journal_id)
                  .with_for_update(key_share=True))
        precedente = s.execute(
            select(Ecriture.sequence, Ecriture.empreinte).where(
                Ecriture.tenant_id == self.tenant_id,
                Ecriture.journal_code == e.journal_code,
                Ecriture.exercice_id == ex.id,
                Ecriture.poste == self.poste,
                Ecriture.sequence.is_not(None))
            .order_by(Ecriture.sequence.desc()).limit(1)
            .execution_options(**INCLURE_SUPPRIMES)).first()
        sequence = (precedente.sequence + 1) if precedente else 1
        empreinte_precedente = precedente.empreinte if precedente else GENESE
        e.poste = self.poste
        e.sequence = sequence
        e.numero = f"{e.journal_code}-{ex.code}-{self.poste}-{sequence:05d}"
        e.statut = Ecriture.VALIDEE
        e.total = total_d
        e.valide_le = utc_now()
        e.valide_par = self.utilisateur
        e.empreinte_precedente = empreinte_precedente
        e.empreinte = calculer_empreinte(e, lignes, ex.code, empreinte_precedente)
        e.is_synced = False
        s.flush()

    def valider(self, ecriture_id: str, *, session: Session | None = None) -> dict:
        def _t(s: Session) -> dict:
            e = s.get(Ecriture, ecriture_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise EcritureInvalide("Écriture introuvable.")
            lignes = list(e.lignes)
            self._valider(s, e, lignes)
            return resume_ecriture(e, lignes)
        return self._executer_avec_reprise_numerotation(_t, session)

    def _executer_avec_reprise_numerotation(self, travail, session):
        """Deux validations simultanées sur le même poste peuvent viser le
        même numéro de séquence : l'index unique tranche, on rejoue."""
        if session is not None:
            return self._executer(travail, session)
        for essai in range(5):
            try:
                return self._executer(travail)
            except IntegrityError:
                if essai == 4:
                    raise
                time.sleep(0.05 * (essai + 1))
        return None  # pragma: no cover

    def modifier_brouillard(self, ecriture_id: str, *, lignes=None,
                            libelle: str | None = None, date_ecriture=None,
                            reference: str | None = None,
                            session: Session | None = None) -> dict:
        def _t(s: Session) -> dict:
            e = s.get(Ecriture, ecriture_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise EcritureInvalide("Écriture introuvable.")
            if e.statut != Ecriture.BROUILLARD:
                raise EcritureVerrouillee(
                    "Écriture validée : corrigez-la par contre-passation.")
            if date_ecriture is not None:
                jour = _date(date_ecriture)
                e.exercice_id = self._controler_date(s, jour).id
                e.date_ecriture = jour
            if libelle is not None:
                e.libelle = _texte(libelle, champ="libellé",
                                   max_len=LONGUEUR_MAX_LIBELLE)
            if reference is not None:
                e.reference = _texte(reference, champ="référence", max_len=60,
                                     obligatoire=False)
            actuelles = list(e.lignes)
            if lignes is not None:
                preparees, total = self._preparer_lignes(s, lignes, e.libelle)
                marquer_supprime(*actuelles)
                s.flush()
                actuelles = self._ajouter_lignes(s, e, preparees, e.libelle)
                e.total = total
            e.is_synced = False
            s.flush()
            s.expire(e, ["lignes"])
            return resume_ecriture(e, actuelles)
        return self._executer(_t, session)

    def supprimer_brouillard(self, ecriture_id: str, *,
                             session: Session | None = None) -> None:
        def _t(s: Session) -> None:
            e = s.get(Ecriture, ecriture_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise EcritureInvalide("Écriture introuvable.")
            if e.statut != Ecriture.BROUILLARD:
                raise EcritureVerrouillee(
                    "Écriture validée : suppression impossible, contre-passez-la.")
            marquer_supprime(e, *e.lignes)
        self._executer(_t, session)

    def ecriture(self, ecriture_id: str, *, session: Session | None = None) -> dict:
        def _t(s: Session) -> dict:
            e = s.get(Ecriture, ecriture_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise EcritureInvalide("Écriture introuvable.")
            return resume_ecriture(e, e.lignes)
        return self._lire(_t, session)

    def ecritures(self, *, debut=None, fin=None, journal: str | None = None,
                  statut: str | None = Ecriture.VALIDEE,
                  source_type: str | None = None, source_id: str | None = None,
                  session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            req = select(Ecriture).where(Ecriture.tenant_id == self.tenant_id)
            if debut is not None:
                req = req.where(Ecriture.date_ecriture >= _date(debut))
            if fin is not None:
                req = req.where(Ecriture.date_ecriture <= _date(fin))
            if journal:
                req = req.where(Ecriture.journal_code == journal.upper())
            if statut:
                req = req.where(Ecriture.statut == statut)
            if source_type:
                req = req.where(Ecriture.source_type == source_type)
            if source_id:
                req = req.where(Ecriture.source_id == source_id)
            req = req.order_by(Ecriture.date_ecriture, Ecriture.journal_code,
                               Ecriture.poste, Ecriture.sequence)
            return [resume_ecriture(e, e.lignes) for e in s.scalars(req)]
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Contre-passation
    # ------------------------------------------------------------------

    def contrepasser(self, ecriture_id: str, *, date_ecriture=None,
                     motif: str | None = None,
                     session: Session | None = None) -> dict:
        """Annule une écriture validée par l'écriture inverse (débit <-> crédit).

        L'original reste intact dans le grand livre. Les lignes lettrables de
        l'original sont délettrées puis lettrées avec leur inverse : la
        facture annulée sort des impayés, les règlements déjà reçus
        redeviennent disponibles (avoir du client)."""

        def _t(s: Session) -> dict:
            e = s.get(Ecriture, ecriture_id)
            if e is None or e.tenant_id != self.tenant_id:
                raise EcritureInvalide("Écriture introuvable.")
            if e.statut != Ecriture.VALIDEE:
                raise EcritureInvalide(
                    "Seule une écriture validée se contre-passe (un brouillard "
                    "se modifie ou se supprime).")
            if self._est_contrepassee(s, e.id):
                raise DejaContrepassee(f"L'écriture {e.numero} est déjà contre-passée.")
            jour = _date(date_ecriture) if date_ecriture is not None else date.today()
            if jour < e.date_ecriture:
                raise EcritureInvalide(
                    "La contre-passation ne peut précéder l'écriture d'origine.")
            libelle = _texte(
                f"Contre-passation {e.numero}" + (f" — {motif}" if motif else ""),
                champ="libellé", max_len=LONGUEUR_MAX_LIBELLE)
            originales = list(e.lignes)
            inverses = [Ligne(compte=lg.compte_numero, debit=lg.credit,
                              credit=lg.debit, libelle=lg.libelle,
                              tiers_ref=lg.tiers_ref, tiers_nom=lg.tiers_nom,
                              date_echeance=lg.date_echeance,
                              taxe_code=lg.taxe_code, base_taxe=lg.base_taxe,
                              devise=lg.devise, montant_devise=lg.montant_devise,
                              section=lg.section)
                        for lg in originales]
            j = self._journal(s, e.journal_code)
            ex = self._controler_date(s, jour)
            preparees, total = self._preparer_lignes(s, inverses, libelle)
            inverse = Ecriture(
                tenant_id=self.tenant_id, journal_id=j.id, journal_code=j.code,
                exercice_id=ex.id, date_ecriture=jour, libelle=libelle,
                reference=e.reference, statut=Ecriture.BROUILLARD,
                type_ecriture=Ecriture.CONTREPASSATION,
                source_type=e.source_type, source_id=e.source_id,
                document_lie=e.document_lie,
                contrepasse_id=e.id, total=total, poste=self.poste,
                cree_par=self.utilisateur)
            s.add(inverse)
            s.flush()
            crees = self._ajouter_lignes(s, inverse, preparees, libelle)
            s.flush()
            self._valider(s, inverse, crees)
            # Lettrage original <-> inverse sur les comptes lettrables.
            meme_exercice = ex.id == e.exercice_id
            for orig, inv in zip(originales, crees):
                compte = self._compte_par_numero(s, orig.compte_numero)
                if compte is None or not compte.lettrable:
                    continue
                if not meme_exercice:
                    # L'original est dans un exercice clos : son solde a été
                    # repris en à-nouveau, c'est avec lui que l'inverse se lettre.
                    self._lettrer_si_solde(s, orig.compte_numero, orig.tiers_ref)
                    continue
                code = self._lettrage_de(s, orig.id)
                if code is not None:
                    self._delettrer(s, code)
                self._lettrer(s, [orig.id, inv.id])
            return resume_ecriture(inverse, crees)

        return self._executer_avec_reprise_numerotation(_t, session)

    def annuler_source(self, source_type: str, source_id: str, *,
                       date_ecriture=None, motif: str | None = None,
                       session: Session | None = None) -> list[dict]:
        """Contre-passe TOUTES les écritures vivantes d'un document métier
        (ex. une vente annulée : facture ET règlement). Idempotent."""
        def _t(s: Session) -> list[dict]:
            vivantes = [e for e in s.scalars(select(Ecriture).where(
                Ecriture.tenant_id == self.tenant_id,
                or_(and_(Ecriture.source_type == source_type,
                         Ecriture.source_id == source_id),
                    and_(Ecriture.source_type.startswith(f"{source_type}."),
                         Ecriture.document_lie == source_id)),
                Ecriture.statut == Ecriture.VALIDEE,
                Ecriture.type_ecriture != Ecriture.CONTREPASSATION))
                if not self._est_contrepassee(s, e.id)]
            vivantes.sort(key=lambda e: (e.date_ecriture, e.valide_le or utc_now()),
                          reverse=True)
            return [self.contrepasser(e.id, date_ecriture=date_ecriture,
                                      motif=motif, session=s)
                    for e in vivantes]
        return self._executer(_t, session)

    # ------------------------------------------------------------------
    # Lettrage
    # ------------------------------------------------------------------

    def _compte_par_numero(self, s: Session, numero: str) -> Compte | None:
        return s.scalar(select(Compte).where(
            Compte.tenant_id == self.tenant_id, Compte.numero == numero))

    def _lettrage_de(self, s: Session, ligne_id: str) -> str | None:
        return s.scalar(select(Lettrage.code)
                        .join(LigneLettrage, LigneLettrage.lettrage_id == Lettrage.id)
                        .where(LigneLettrage.ligne_id == ligne_id,
                               Lettrage.tenant_id == self.tenant_id))

    def _lettrer(self, s: Session, ligne_ids: list[str]) -> str:
        ids = list(dict.fromkeys(ligne_ids))
        if len(ids) < 2:
            raise LettrageInvalide("Un lettrage rapproche au moins deux lignes.")
        lignes = list(s.scalars(select(LigneEcriture).where(
            LigneEcriture.id.in_(ids),
            LigneEcriture.tenant_id == self.tenant_id)))
        if len(lignes) != len(ids):
            raise LettrageInvalide("Ligne(s) introuvable(s).")
        comptes = {lg.compte_numero for lg in lignes}
        if len(comptes) != 1:
            raise LettrageInvalide("Toutes les lignes doivent porter le même compte.")
        tiers = {lg.tiers_ref for lg in lignes}
        if len(tiers) != 1:
            raise LettrageInvalide("Toutes les lignes doivent concerner le même tiers.")
        numero = comptes.pop()
        compte = self._compte_par_numero(s, numero)
        if compte is None or not compte.lettrable:
            raise LettrageInvalide(f"Le compte {numero} n'est pas lettrable.")
        statuts = set(s.scalars(select(Ecriture.statut).where(
            Ecriture.id.in_({lg.ecriture_id for lg in lignes}))))
        if statuts != {Ecriture.VALIDEE}:
            raise LettrageInvalide("Seules des lignes validées se lettrent.")
        for lg in lignes:
            if self._lettrage_de(s, lg.id) is not None:
                raise LettrageInvalide(
                    f"Une ligne ({lg.libelle}) est déjà lettrée.")
        solde = sum((lg.solde for lg in lignes), ZERO)
        if solde != ZERO:
            raise LettrageInvalide(
                f"Lignes non soldées (écart {solde}) : le lettrage exige un "
                "solde nul.")
        lettrage = Lettrage(tenant_id=self.tenant_id, code="", compte_numero=numero,
                            tiers_ref=tiers.pop(), cree_par=self.utilisateur)
        lettrage.code = f"{self.poste}-{_court(lettrage)}"
        s.add(lettrage)
        s.flush()
        for lg in lignes:
            s.add(LigneLettrage(tenant_id=self.tenant_id,
                                lettrage_id=lettrage.id, ligne_id=lg.id))
        s.flush()
        return lettrage.code

    def lettrer(self, ligne_ids: list[str], *,
                session: Session | None = None) -> str:
        """Rapproche des lignes soldées entre elles. Renvoie le code de lettrage."""
        return self._executer(lambda s: self._lettrer(s, ligne_ids), session)

    def _delettrer(self, s: Session, code: str) -> None:
        lettrage = s.scalar(select(Lettrage).where(
            Lettrage.tenant_id == self.tenant_id, Lettrage.code == code))
        if lettrage is None:
            raise LettrageInvalide(f"Lettrage {code!r} introuvable.")
        liens = list(s.scalars(select(LigneLettrage).where(
            LigneLettrage.lettrage_id == lettrage.id)))
        marquer_supprime(lettrage, *liens)
        s.flush()

    def delettrer(self, code: str, *, session: Session | None = None) -> None:
        self._executer(lambda s: self._delettrer(s, code), session)

    def _exercices_ouverts(self, s: Session):
        """Sous-requête des exercices ouverts. Les lignes d'un exercice clos
        ne se lettrent plus : leurs soldes vivent dans les à-nouveaux."""
        return select(Exercice.id).where(Exercice.tenant_id == self.tenant_id,
                                         Exercice.statut == Exercice.OUVERT)

    def _lignes_ouvertes(self, s: Session, compte: str, tiers_ref: str | None
                         ) -> list[LigneEcriture]:
        lettrees = select(LigneLettrage.ligne_id).where(
            LigneLettrage.tenant_id == self.tenant_id,
            LigneLettrage.deleted_at.is_(None))
        req = (select(LigneEcriture)
               .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
               .where(LigneEcriture.tenant_id == self.tenant_id,
                      LigneEcriture.compte_numero == compte,
                      Ecriture.statut == Ecriture.VALIDEE,
                      Ecriture.exercice_id.in_(self._exercices_ouverts(s)),
                      LigneEcriture.id.not_in(lettrees))
               .order_by(Ecriture.date_ecriture, LigneEcriture.id))
        if tiers_ref is None:
            req = req.where(LigneEcriture.tiers_ref.is_(None))
        else:
            req = req.where(LigneEcriture.tiers_ref == tiers_ref)
        return list(s.scalars(req))

    def lignes_non_lettrees(self, compte: str, *, tiers_ref: str | None = None,
                            session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            return [{"id": lg.id, "ecriture_id": lg.ecriture_id,
                     "compte": lg.compte_numero, "libelle": lg.libelle,
                     "debit": lg.debit, "credit": lg.credit,
                     "tiers_ref": lg.tiers_ref, "date_echeance": lg.date_echeance}
                    for lg in self._lignes_ouvertes(s, str(compte), tiers_ref)]
        return self._lire(_t, session)

    def _lettrer_si_solde(self, s: Session, compte: str, tiers_ref: str | None
                          ) -> str | None:
        """Lettre toutes les lignes ouvertes d'un tiers si leur solde est nul."""
        ouvertes = self._lignes_ouvertes(s, compte, tiers_ref)
        if len(ouvertes) >= 2 and sum((lg.solde for lg in ouvertes), ZERO) == ZERO:
            return self._lettrer(s, [lg.id for lg in ouvertes])
        return None

    def lettrage_automatique(self, compte: str, *, tiers_ref: str | None = None,
                             session: Session | None = None) -> str | None:
        return self._executer(
            lambda s: self._lettrer_si_solde(s, str(compte), tiers_ref), session)

    # ------------------------------------------------------------------
    # Intégrité et contrôles
    # ------------------------------------------------------------------

    def verifier_integrite(self, *, session: Session | None = None) -> dict:
        """Recalcule toutes les chaînes d'empreintes du tenant.

        Renvoie ``{"ok": bool, "ecritures": n, "chaines": n, "anomalies": [...]}``.
        Une anomalie désigne l'écriture où la chaîne casse : modification
        directe en base, ligne supprimée, séquence trouée."""
        def _t(s: Session) -> dict:
            exercices = {e.id: e.code for e in s.scalars(
                select(Exercice).where(Exercice.tenant_id == self.tenant_id)
                .execution_options(**INCLURE_SUPPRIMES))}
            ecritures = list(s.scalars(select(Ecriture).where(
                Ecriture.tenant_id == self.tenant_id,
                Ecriture.statut == Ecriture.VALIDEE)
                .execution_options(**INCLURE_SUPPRIMES)))
            lignes_par_ecriture: dict[str, list[LigneEcriture]] = {}
            for lg in s.scalars(select(LigneEcriture).where(
                    LigneEcriture.tenant_id == self.tenant_id,
                    LigneEcriture.deleted_at.is_(None))
                    .execution_options(**INCLURE_SUPPRIMES)):
                lignes_par_ecriture.setdefault(lg.ecriture_id, []).append(lg)
            chaines: dict[tuple, list[Ecriture]] = {}
            for e in ecritures:
                chaines.setdefault(
                    (e.journal_code, e.exercice_id, e.poste), []).append(e)
            anomalies = []
            for (journal, exercice_id, poste), liste in chaines.items():
                liste.sort(key=lambda x: x.sequence or 0)
                precedente = GENESE
                attendu = 1
                for e in liste:
                    if e.deleted_at is not None:
                        anomalies.append({"numero": e.numero,
                                          "anomalie": "écriture validée supprimée"})
                    if e.sequence != attendu:
                        anomalies.append({"numero": e.numero,
                                          "anomalie": f"séquence {e.sequence}, "
                                                      f"attendu {attendu}"})
                        attendu = (e.sequence or attendu)
                    lignes = lignes_par_ecriture.get(e.id, [])
                    d = sum((lg.debit for lg in lignes), ZERO)
                    c = sum((lg.credit for lg in lignes), ZERO)
                    if d != c or d != e.total:
                        anomalies.append({"numero": e.numero,
                                          "anomalie": "écriture déséquilibrée "
                                                      "ou total altéré"})
                    if e.empreinte_precedente != precedente:
                        anomalies.append({"numero": e.numero,
                                          "anomalie": "chaînage rompu"})
                    calcul = calculer_empreinte(
                        e, lignes, exercices.get(exercice_id, "?"),
                        e.empreinte_precedente or GENESE)
                    if calcul != e.empreinte:
                        anomalies.append({"numero": e.numero,
                                          "anomalie": "contenu modifié après "
                                                      "validation"})
                    precedente = e.empreinte
                    attendu += 1
            return {"ok": not anomalies, "ecritures": len(ecritures),
                    "chaines": len(chaines), "anomalies": anomalies}
        return self._lire(_t, session)

    def controles(self, *, session: Session | None = None) -> list[dict]:
        """Contrôles de cohérence (à lancer après une synchronisation) :
        contre-passations multiples, doublons de génération par source,
        lettrages concurrents d'une même ligne, brouillards anciens."""
        def _t(s: Session) -> list[dict]:
            constats = []
            for cible, nb in s.execute(
                    select(Ecriture.contrepasse_id, func.count(Ecriture.id))
                    .where(Ecriture.tenant_id == self.tenant_id,
                           Ecriture.contrepasse_id.is_not(None))
                    .group_by(Ecriture.contrepasse_id)
                    .having(func.count(Ecriture.id) > 1)):
                constats.append({"controle": "contre-passation multiple",
                                 "ecriture_id": cible, "nombre": nb})
            vivantes: dict[tuple, int] = {}
            contrepassees = set(s.scalars(select(Ecriture.contrepasse_id).where(
                Ecriture.tenant_id == self.tenant_id,
                Ecriture.contrepasse_id.is_not(None))))
            for e in s.scalars(select(Ecriture).where(
                    Ecriture.tenant_id == self.tenant_id,
                    Ecriture.source_type.is_not(None),
                    Ecriture.type_ecriture != Ecriture.CONTREPASSATION)):
                if e.id not in contrepassees:
                    cle = (e.source_type, e.source_id)
                    vivantes[cle] = vivantes.get(cle, 0) + 1
            for (st, sid), nb in vivantes.items():
                if nb > 1:
                    constats.append({"controle": "source comptabilisée plusieurs fois",
                                     "source_type": st, "source_id": sid,
                                     "nombre": nb})
            for ligne_id, nb in s.execute(
                    select(LigneLettrage.ligne_id, func.count(LigneLettrage.id))
                    .where(LigneLettrage.tenant_id == self.tenant_id)
                    .group_by(LigneLettrage.ligne_id)
                    .having(func.count(LigneLettrage.id) > 1)):
                constats.append({"controle": "ligne lettrée plusieurs fois",
                                 "ligne_id": ligne_id, "nombre": nb})
            verrou = self._date_verrouillage(s)
            if verrou:
                nb = s.scalar(select(func.count(Ecriture.id)).where(
                    Ecriture.tenant_id == self.tenant_id,
                    Ecriture.statut == Ecriture.BROUILLARD,
                    Ecriture.date_ecriture <= verrou))
                if nb:
                    constats.append({"controle": "brouillard en période verrouillée",
                                     "nombre": nb})
            return constats
        return self._lire(_t, session)


def _code_taux(taux: Decimal) -> str:
    """18.00 -> '18' ; 5.50 -> '5.5'."""
    texte = format(taux.normalize(), "f")
    return texte


def _court(objet) -> str:
    from .socle import generate_uuid
    if not getattr(objet, "id", None):
        objet.id = generate_uuid()
    return objet.id.replace("-", "")[:8].upper()
