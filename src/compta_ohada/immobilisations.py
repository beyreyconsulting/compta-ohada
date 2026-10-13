"""
Immobilisations et amortissements automatiques.

    compta.creer_immobilisation("INFO-001", "Ordinateur portable", "2442",
                                valeur_origine="900000", date_acquisition="2026-06-01",
                                duree_mois=36, contrepartie="481", tiers_ref="F2")
    compta.comptabiliser_dotations("2026-12-31")     # une OD pour tout le parc
    compta.ceder_immobilisation("INFO-001", "2027-09-30", prix="300000",
                                compte_tresorerie="5711")

Règles de calcul :

- **Linéaire**, prorata temporis en jours sur la base d'une année de 360 jours
  (mois de 30 jours), à partir de la date de mise en service.
- **Dégressif**, prorata en mois entiers à partir du mois d'acquisition ;
  taux linéaire × coefficient, puis bascule en linéaire sur la durée restante
  quand celui-ci devient supérieur. Coefficients par défaut : 1,5 (3 et 4 ans),
  2 (5 et 6 ans), 2,5 (au-delà) — barème à confirmer pour chaque pays
  (Code général des impôts), modifiable par fiche.
- Les dotations ne sont pas stockées sur la fiche : elles sont dans le grand
  livre (ligne du compte 28x portant ``tiers_ref = code``). Le cumul pratiqué
  est toujours DÉRIVÉ, ce qui rend ``comptabiliser_dotations`` idempotent et
  sûr en synchronisation : il passe exactement ce qui manque à la date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .erreurs import CompteInconnu, EcritureInvalide, ErreurComptable
from .modeles import Compte, Ecriture, Immobilisation, LigneEcriture, id_deterministe
from .noyau import Ligne, _date, _texte, resume_ecriture
from .socle import CENTIME, INCLURE_SUPPRIMES, ZERO, montant


def _jours_30_360(debut: date, fin: date) -> int:
    """Nombre de jours de ``debut`` à ``fin`` INCLUS, en convention 30/360."""
    d1, d2 = min(debut.day, 30), min(fin.day, 30)
    if fin.month == 2 and fin == (date(fin.year, 3, 1) - timedelta(days=1)):
        d2 = 30                      # fin février = fin de mois
    return ((fin.year - debut.year) * 360 + (fin.month - debut.month) * 30
            + (d2 - d1) + 1)


def coefficient_degressif(duree_mois: int) -> Decimal:
    annees = duree_mois / 12
    if annees < 3:
        raise EcritureInvalide("Le dégressif suppose une durée d'au moins 3 ans.")
    if annees <= 4:
        return Decimal("1.5")
    if annees <= 6:
        return Decimal("2")
    return Decimal("2.5")


@dataclass(frozen=True)
class Annuite:
    exercice: int
    debut: date
    fin: date
    base: Decimal
    dotation: Decimal
    cumul: Decimal
    vnc: Decimal


def plan(immo: Immobilisation) -> list[Annuite]:
    """Plan d'amortissement par année civile, de la mise en service à la fin."""
    vo = Decimal(immo.valeur_origine)
    vr = Decimal(immo.valeur_residuelle or 0)
    base = vo - vr
    if immo.duree_mois <= 0 or base <= 0:
        return []
    if immo.methode == Immobilisation.DEGRESSIF:
        return _plan_degressif(immo, vo, base)
    debut = immo.date_mise_en_service
    fin_plan = _fin_de_plan(debut, immo.duree_mois)
    total_jours = _jours_30_360(debut, fin_plan)
    annuites = []
    cumul = ZERO
    annee = debut.year
    while True:
        d = max(debut, date(annee, 1, 1))
        f = min(fin_plan, date(annee, 12, 31))
        if d > f:
            break
        jours = _jours_30_360(d, f)
        dotation = (base * jours / total_jours).quantize(CENTIME, rounding=ROUND_HALF_UP)
        if f == fin_plan:
            dotation = base - cumul          # dernière annuité : solde exact
        cumul += dotation
        annuites.append(Annuite(annee, d, f, base, dotation, cumul, vo - cumul))
        if f == fin_plan:
            break
        annee += 1
    return annuites


def _fin_de_plan(debut: date, duree_mois: int) -> date:
    """Dernier jour du plan : début + durée (en mois calendaires) − 1 jour."""
    mois = debut.month - 1 + duree_mois
    annee, mois = debut.year + mois // 12, mois % 12 + 1
    jour = debut.day
    while True:
        try:
            return date(annee, mois, jour) - timedelta(days=1)
        except ValueError:            # 31 -> 30 -> 29 -> 28
            jour -= 1


def _plan_degressif(immo: Immobilisation, vo: Decimal, base: Decimal) -> list[Annuite]:
    coef = (Decimal(immo.coefficient_pc) / 100 if immo.coefficient_pc
            else coefficient_degressif(immo.duree_mois))
    taux = Decimal(12) / Decimal(immo.duree_mois) * coef
    acquisition = immo.date_acquisition
    mois_premiere = 12 - acquisition.month + 1
    mois_restants = immo.duree_mois
    vnc = base
    cumul = ZERO
    annuites = []
    annee = acquisition.year
    premiere = True
    while vnc > 0 and mois_restants > 0:
        mois = min(mois_premiere if premiere else 12, mois_restants)
        annees_restantes = Decimal(mois_restants) / 12
        lineaire = vnc / annees_restantes
        taux_annuel = max(taux, Decimal(1) / annees_restantes)
        annuelle = vnc * taux_annuel if taux_annuel == taux else lineaire
        dotation = (annuelle * mois / 12).quantize(CENTIME, rounding=ROUND_HALF_UP)
        mois_restants -= mois
        if mois_restants <= 0 or dotation >= vnc:
            dotation = vnc
        cumul += dotation
        vnc -= dotation
        d = date(annee, acquisition.month, 1) if premiere else date(annee, 1, 1)
        annuites.append(Annuite(annee, d, date(annee, 12, 31), base, dotation,
                                cumul, vo - cumul))
        annee += 1
        premiere = False
    return annuites


def cumul_theorique(immo: Immobilisation, jour: date) -> Decimal:
    """Amortissement cumulé que le plan prévoit au ``jour`` (inclus)."""
    total = ZERO
    for a in plan(immo):
        if a.fin <= jour:
            total = a.cumul
            continue
        if a.debut > jour:
            break
        if immo.methode == Immobilisation.DEGRESSIF:
            mois_total = (a.fin.year - a.debut.year) * 12 + a.fin.month - a.debut.month + 1
            mois_ecoules = jour.month - a.debut.month + 1
            part = (a.dotation * mois_ecoules / mois_total)
        else:
            part = a.dotation * _jours_30_360(a.debut, jour) / _jours_30_360(a.debut, a.fin)
        return (total + part).quantize(CENTIME, rounding=ROUND_HALF_UP)
    return total


class ImmobilisationsMixin:
    """Fiches d'immobilisations, dotations, cessions de ``Comptabilite``."""

    # ------------------------------------------------------------------
    # Fiches
    # ------------------------------------------------------------------

    def _compte_existant(self, s: Session, candidats: list[str]) -> str | None:
        for numero in candidats:
            c = s.scalar(select(Compte).where(
                Compte.tenant_id == self.tenant_id, Compte.numero == numero,
                Compte.actif.is_(True), Compte.imputable.is_(True)))
            if c is not None:
                return c.numero
        return None

    def _immo(self, s: Session, code: str) -> Immobilisation:
        immo = s.get(Immobilisation, id_deterministe(self.tenant_id, "immobilisation",
                                                     str(code).strip().upper()))
        if immo is None or immo.tenant_id != self.tenant_id:
            raise EcritureInvalide(f"Immobilisation {code!r} introuvable.")
        return immo

    def creer_immobilisation(self, code: str, libelle: str, compte_immo: str, *,
                             valeur_origine, date_acquisition, duree_mois: int,
                             date_mise_en_service=None, methode: str = "LINEAIRE",
                             coefficient=None, valeur_residuelle=0,
                             compte_amort: str | None = None,
                             compte_dotation: str | None = None,
                             amortissements_anterieurs=0,
                             contrepartie: str | None = None,
                             tiers_ref: str | None = None,
                             tiers_nom: str | None = None,
                             session: Session | None = None) -> dict:
        """Crée la fiche et, si ``contrepartie`` est donnée (481, 5211…),
        l'écriture d'acquisition D 2x / C contrepartie.

        Pour une reprise d'existant (client qui démarre sur le logiciel avec
        un parc déjà amorti), passer ``amortissements_anterieurs`` : ils sont
        supposés déjà présents au solde du 28x (à-nouveaux)."""
        code = _texte(code, champ="code", max_len=30).upper()
        libelle = _texte(libelle, champ="libellé", max_len=200)
        vo = montant(valeur_origine, champ="valeur d'origine")
        vr = montant(valeur_residuelle or 0, champ="valeur résiduelle")
        anterieurs = montant(amortissements_anterieurs or 0,
                             champ="amortissements antérieurs")
        acquisition = _date(date_acquisition, champ="date d'acquisition")
        service = _date(date_mise_en_service or acquisition,
                        champ="date de mise en service")
        duree = int(duree_mois)
        if vo <= 0:
            raise EcritureInvalide("La valeur d'origine est strictement positive.")
        if vr < 0 or vr >= vo:
            raise EcritureInvalide("Valeur résiduelle hors bornes.")
        if anterieurs < 0 or anterieurs > vo - vr:
            raise EcritureInvalide("Amortissements antérieurs hors bornes.")
        if service < acquisition:
            raise EcritureInvalide("La mise en service suit l'acquisition.")
        if duree < 0 or duree > 1200:
            raise EcritureInvalide("Durée d'amortissement hors bornes (0 à 100 ans).")
        methode = str(methode or "").upper()
        if methode not in (Immobilisation.LINEAIRE, Immobilisation.DEGRESSIF):
            raise EcritureInvalide("Méthode : LINEAIRE ou DEGRESSIF.")
        coef_pc = None
        if methode == Immobilisation.DEGRESSIF:
            coef = (Decimal(str(coefficient)) if coefficient is not None
                    else coefficient_degressif(duree))
            if coef <= 1 or coef > 5:
                raise EcritureInvalide("Coefficient dégressif hors bornes (> 1, ≤ 5).")
            coef_pc = int((coef * 100).to_integral_value())

        def _t(s: Session) -> dict:
            compte = self._compte(s, compte_immo, pour_imputation=True)
            if compte.classe != 2 or compte.numero.startswith(("28", "29")):
                raise CompteInconnu(
                    "Une immobilisation se porte sur un compte 21 à 27.")
            non_amortissable = compte.numero.startswith(("22", "25", "26", "27"))
            if duree == 0 and not non_amortissable:
                raise EcritureInvalide(
                    "Durée nulle réservée aux biens non amortissables (terrains, "
                    "avances, immobilisations financières).")
            ca = compte_amort
            if duree and ca is None:
                suffixe = compte.numero[1:]
                ca = self._compte_existant(
                    s, ["28" + suffixe[:i] for i in range(len(suffixe), 0, -1)])
                if ca is None:
                    raise CompteInconnu(
                        f"Aucun compte d'amortissement pour {compte.numero} : "
                        "précisez compte_amort.")
            if ca is not None:
                if not self._compte(s, ca, pour_imputation=True).numero.startswith("28"):
                    raise CompteInconnu("Le compte d'amortissement est un 28x.")
            cd = compte_dotation
            if duree and cd is None:
                cd = self._compte_existant(
                    s, ["6812", "681"] if compte.numero.startswith("21")
                    else ["6813", "681"])
            if cd is not None:
                if not self._compte(s, cd, pour_imputation=True).numero.startswith("68"):
                    raise CompteInconnu("Le compte de dotation est un 68x.")
            iid = id_deterministe(self.tenant_id, "immobilisation", code)
            if s.get(Immobilisation, iid, execution_options=INCLURE_SUPPRIMES):
                raise EcritureInvalide(f"L'immobilisation {code} existe déjà.")
            immo = Immobilisation(
                id=iid, tenant_id=self.tenant_id, code=code, libelle=libelle,
                compte_immo=compte.numero, compte_amort=ca, compte_dotation=cd,
                valeur_origine=vo, valeur_residuelle=vr,
                amortissements_anterieurs=anterieurs, date_acquisition=acquisition,
                date_mise_en_service=service, duree_mois=duree, methode=methode,
                coefficient_pc=coef_pc, statut=Immobilisation.EN_SERVICE)
            s.add(immo)
            s.flush()
            ecriture = None
            if contrepartie:
                ecriture = self._saisir(
                    self._parametre(s, "journal_od"), acquisition,
                    f"Acquisition {libelle}",
                    [Ligne(compte=compte.numero, debit=vo, tiers_ref=code),
                     Ligne(compte=contrepartie, credit=vo, tiers_ref=tiers_ref,
                           tiers_nom=tiers_nom)],
                    reference=code, valider=True, source_type="immobilisation",
                    source_id=code, type_ecriture=Ecriture.NORMALE, session=s)
            return {**self._immo_dict(s, immo, acquisition), "acquisition": ecriture}

        return self._executer_avec_reprise_numerotation(_t, session)

    def _cumul_comptabilise(self, s: Session, immo: Immobilisation,
                            jour: date | None = None) -> Decimal:
        if not immo.compte_amort:
            return ZERO
        req = (select(func.coalesce(func.sum(LigneEcriture.credit), 0),
                      func.coalesce(func.sum(LigneEcriture.debit), 0))
               .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
               .where(LigneEcriture.tenant_id == self.tenant_id,
                      LigneEcriture.deleted_at.is_(None),
                      LigneEcriture.compte_numero == immo.compte_amort,
                      LigneEcriture.tiers_ref == immo.code,
                      Ecriture.statut == Ecriture.VALIDEE,
                      Ecriture.type_ecriture != Ecriture.A_NOUVEAU))
        if jour is not None:
            req = req.where(Ecriture.date_ecriture <= jour)
        from .etats import _dec
        c, d = s.execute(req).one()
        return Decimal(immo.amortissements_anterieurs or 0) + _dec(c) - _dec(d)

    def _immo_dict(self, s: Session, immo: Immobilisation, jour: date) -> dict:
        cumul = self._cumul_comptabilise(s, immo, jour)
        vo = Decimal(immo.valeur_origine)
        sortie = immo.statut != Immobilisation.EN_SERVICE
        return {"code": immo.code, "libelle": immo.libelle,
                "compte": immo.compte_immo, "compte_amort": immo.compte_amort,
                "compte_dotation": immo.compte_dotation, "valeur_origine": vo,
                "valeur_residuelle": Decimal(immo.valeur_residuelle or 0),
                "date_acquisition": immo.date_acquisition,
                "date_mise_en_service": immo.date_mise_en_service,
                "duree_mois": immo.duree_mois, "methode": immo.methode,
                "coefficient": (Decimal(immo.coefficient_pc) / 100
                                if immo.coefficient_pc else None),
                "statut": immo.statut, "date_sortie": immo.date_sortie,
                "prix_cession": immo.prix_cession,
                "amortissements_cumules": ZERO if sortie else cumul,
                "vnc": ZERO if sortie else vo - cumul}

    def immobilisations(self, *, au=None, inclure_sorties: bool = False,
                        session: Session | None = None) -> list[dict]:
        jour = _date(au) if au else date.today()

        def _t(s: Session) -> list[dict]:
            req = select(Immobilisation).where(
                Immobilisation.tenant_id == self.tenant_id)
            if not inclure_sorties:
                req = req.where(Immobilisation.statut == Immobilisation.EN_SERVICE)
            return [self._immo_dict(s, i, jour)
                    for i in s.scalars(req.order_by(Immobilisation.code))]
        return self._lire(_t, session)

    def plan_amortissement(self, code: str, *,
                           session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            return [a.__dict__ for a in plan(self._immo(s, code))]
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Dotations
    # ------------------------------------------------------------------

    def _lignes_dotation(self, s: Session, immo: Immobilisation, jour: date
                         ) -> list[Ligne]:
        if not immo.duree_mois or not immo.compte_amort:
            return []
        if immo.date_mise_en_service > jour:
            return []
        manque = cumul_theorique(immo, jour) - self._cumul_comptabilise(s, immo)
        if manque <= 0:
            return []
        lib = f"Dotation {immo.libelle}"
        return [Ligne(compte=immo.compte_dotation, debit=manque, libelle=lib),
                Ligne(compte=immo.compte_amort, credit=manque, libelle=lib,
                      tiers_ref=immo.code, tiers_nom=immo.libelle)]

    def comptabiliser_dotations(self, date_arrete, *,
                                session: Session | None = None) -> dict | None:
        """Passe en une écriture (journal OD) toutes les dotations manquantes
        à la date — mensuelle, trimestrielle ou annuelle, au choix. Idempotent."""
        jour = _date(date_arrete)

        def _t(s: Session) -> dict | None:
            existante = self._ecriture_source(s, "amortissements", jour.isoformat())
            if existante is not None:
                return resume_ecriture(existante, existante.lignes)
            lignes: list[Ligne] = []
            for immo in s.scalars(select(Immobilisation).where(
                    Immobilisation.tenant_id == self.tenant_id,
                    Immobilisation.statut == Immobilisation.EN_SERVICE)
                    .order_by(Immobilisation.code)):
                lignes += self._lignes_dotation(s, immo, jour)
            if not lignes:
                return None
            return self._saisir(self._parametre(s, "journal_od"), jour,
                                f"Dotations aux amortissements au {jour:%d/%m/%Y}",
                                lignes, reference=None, valider=True,
                                source_type="amortissements",
                                source_id=jour.isoformat(),
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)

    # ------------------------------------------------------------------
    # Sorties
    # ------------------------------------------------------------------

    def ceder_immobilisation(self, code: str, date_cession, *, prix=0,
                             compte_tresorerie: str | None = None,
                             tiers_ref: str | None = None,
                             tiers_nom: str | None = None,
                             session: Session | None = None) -> dict:
        """Cession (ou mise au rebut si ``prix`` = 0).

        1. dotation complémentaire jusqu'à la date de sortie ;
        2. sortie du bien : D 28x (cumul) + D 81x (valeur nette) / C 2x ;
        3. produit de cession : D 485 / C 82x ; encaissement immédiat si
           ``compte_tresorerie`` (D 5x / C 485)."""
        jour = _date(date_cession)
        prix = montant(prix or 0, champ="prix de cession")
        if prix < 0:
            raise EcritureInvalide("Le prix de cession est positif ou nul.")

        def _t(s: Session) -> dict:
            immo = self._immo(s, code)
            if immo.statut != Immobilisation.EN_SERVICE:
                raise ErreurComptable(f"L'immobilisation {immo.code} est déjà sortie.")
            if jour < immo.date_acquisition:
                raise EcritureInvalide("Sortie antérieure à l'acquisition.")
            od = self._parametre(s, "journal_od")
            resultat = {"dotation": None, "sortie": None, "cession": None,
                        "encaissement": None}
            lignes_dot = self._lignes_dotation(s, immo, jour)
            if lignes_dot:
                resultat["dotation"] = self._saisir(
                    od, jour, f"Dotation complémentaire — sortie {immo.libelle}",
                    lignes_dot, reference=immo.code, valider=True,
                    source_type="amortissement.sortie", source_id=immo.code,
                    type_ecriture=Ecriture.NORMALE, session=s)
            vo = Decimal(immo.valeur_origine)
            cumul = self._cumul_comptabilise(s, immo)
            vnc = vo - cumul
            classe_vc = {"21": "811", "26": "816", "27": "816"}.get(
                immo.compte_immo[:2], "812")
            classe_pc = {"21": "821", "26": "826", "27": "826"}.get(
                immo.compte_immo[:2], "822")
            lib = (f"Cession {immo.libelle}" if prix else f"Mise au rebut {immo.libelle}")
            lignes = []
            if cumul and immo.compte_amort:
                # Le 28x est soldé pour CE bien ; les amortissements antérieurs
                # à la reprise ont été repris sans tiers, on les solde aussi.
                lignes.append(Ligne(compte=immo.compte_amort, debit=cumul, libelle=lib,
                                    tiers_ref=immo.code, tiers_nom=immo.libelle))
            if vnc:
                lignes.append(Ligne(compte=classe_vc, debit=vnc, libelle=lib))
            lignes.append(Ligne(compte=immo.compte_immo, credit=vo, libelle=lib,
                                tiers_ref=immo.code))
            resultat["sortie"] = self._saisir(
                od, jour, lib, lignes, reference=immo.code, valider=True,
                source_type="immobilisation.sortie", source_id=immo.code,
                type_ecriture=Ecriture.NORMALE, session=s)
            if prix:
                resultat["cession"] = self._saisir(
                    od, jour, f"Prix de cession {immo.libelle}",
                    [Ligne(compte="485", debit=prix, tiers_ref=tiers_ref or immo.code,
                           tiers_nom=tiers_nom),
                     Ligne(compte=classe_pc, credit=prix)],
                    reference=immo.code, valider=True,
                    source_type="immobilisation.cession", source_id=immo.code,
                    type_ecriture=Ecriture.NORMALE, session=s)
                if compte_tresorerie:
                    resultat["encaissement"] = self._reglement(
                        s, montant_=prix, compte_tresorerie=compte_tresorerie,
                        jour=jour, compte_tiers="485",
                        tiers_ref=tiers_ref or immo.code, tiers_nom=tiers_nom,
                        encaissement=True, source_type="immobilisation.reglement",
                        source_id=immo.code, document_lie=immo.code,
                        libelle=f"Encaissement cession {immo.libelle}",
                        reference=immo.code)
                    self._lettrer_si_solde(s, "485", tiers_ref or immo.code)
            immo.statut = Immobilisation.CEDEE if prix else Immobilisation.REBUTEE
            immo.date_sortie = jour
            immo.prix_cession = prix
            immo.is_synced = False
            resultat["plus_ou_moins_value"] = prix - vnc
            return resultat
        return self._executer_avec_reprise_numerotation(_t, session)

    def mettre_au_rebut(self, code: str, date_sortie, *,
                        session: Session | None = None) -> dict:
        return self.ceder_immobilisation(code, date_sortie, prix=0, session=session)

    # ------------------------------------------------------------------
    # Tableau des immobilisations et des amortissements
    # ------------------------------------------------------------------

    def tableau_immobilisations(self, exercice, *,
                                session: Session | None = None) -> dict:
        """Mouvements de l'exercice par bien : valeur brute (début, entrées,
        sorties, fin), amortissements (début, dotations, reprises, fin), VNC."""

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code_ou_date(s, exercice)
            veille = ex.date_debut - timedelta(days=1)
            lignes = []
            totaux = dict.fromkeys(("brut_debut", "entrees", "sorties", "brut_fin",
                                    "amort_debut", "dotations", "reprises",
                                    "amort_fin", "vnc"), ZERO)
            for immo in s.scalars(select(Immobilisation).where(
                    Immobilisation.tenant_id == self.tenant_id)
                    .order_by(Immobilisation.code)):
                if immo.date_acquisition > ex.date_fin:
                    continue
                if immo.date_sortie and immo.date_sortie < ex.date_debut:
                    continue
                vo = Decimal(immo.valeur_origine)
                present_debut = immo.date_acquisition <= veille
                sortie = immo.date_sortie is not None and \
                    ex.date_debut <= immo.date_sortie <= ex.date_fin
                amort_debut = self._cumul_comptabilise(s, immo, veille) \
                    if present_debut else ZERO
                amort_fin_avant_sortie = self._cumul_comptabilise(s, immo, ex.date_fin)
                if sortie:
                    # Le cumul a été soldé à la sortie : on reconstitue
                    # dotations et reprise.
                    dotations = self._dotations_periode(s, immo, ex.date_debut,
                                                        ex.date_fin)
                    reprises = amort_debut + dotations
                    amort_fin = ZERO
                else:
                    dotations = amort_fin_avant_sortie - amort_debut
                    reprises = ZERO
                    amort_fin = amort_fin_avant_sortie
                ligne = {
                    "code": immo.code, "libelle": immo.libelle,
                    "compte": immo.compte_immo,
                    "brut_debut": vo if present_debut else ZERO,
                    "entrees": ZERO if present_debut else vo,
                    "sorties": vo if sortie else ZERO,
                    "brut_fin": ZERO if sortie else vo,
                    "amort_debut": amort_debut, "dotations": dotations,
                    "reprises": reprises, "amort_fin": amort_fin,
                    "vnc": ZERO if sortie else vo - amort_fin,
                }
                lignes.append(ligne)
                for k in totaux:
                    totaux[k] += ligne[k]
            return {"exercice": ex.code, "lignes": lignes, "totaux": totaux}
        return self._lire(_t, session)

    def _dotations_periode(self, s: Session, immo: Immobilisation, debut: date,
                           fin: date) -> Decimal:
        from .etats import _dec
        c = s.scalar(select(func.coalesce(func.sum(LigneEcriture.credit), 0))
                     .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                     .where(LigneEcriture.tenant_id == self.tenant_id,
                            LigneEcriture.deleted_at.is_(None),
                            LigneEcriture.compte_numero == immo.compte_amort,
                            LigneEcriture.tiers_ref == immo.code,
                            Ecriture.statut == Ecriture.VALIDEE,
                            Ecriture.type_ecriture != Ecriture.A_NOUVEAU,
                            Ecriture.date_ecriture >= debut,
                            Ecriture.date_ecriture <= fin))
        return _dec(c)
