"""
Clôture d'exercice, réouverture (à-nouveaux) et affectation du résultat.

Clôturer l'exercice N, en une seule transaction :

1. refuse s'il reste des brouillards, si la chaîne d'intégrité est rompue ou
   si l'exercice est déjà clos ;
2. passe l'écriture de détermination du résultat (journal OD, type CLOTURE,
   datée du dernier jour) : solde les classes 6, 7 et 8 vers 131 (bénéfice)
   ou 139 (perte) ;
3. ouvre l'exercice N+1 s'il n'existe pas et y passe les à-nouveaux (journal
   AN, datés du premier jour) : un solde par compte de bilan, et PAR TIERS
   pour les comptes lettrables (créances et dettes restent nominatives) ;
4. marque N comme clôturé et verrouille la saisie jusqu'à sa date de fin.

Une clôture est définitive : aucune écriture ne peut plus être datée dans N.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .erreurs import ErreurComptable, EcritureInvalide, ExerciceClos
from .etats import CLASSES_GESTION
from .modeles import Compte, Ecriture, Exercice, LigneEcriture, LigneLettrage
from .noyau import Ligne, _date
from .socle import ZERO, montant, utc_now


def _meme_jour_annee_suivante(jour: date) -> date:
    try:
        return jour.replace(year=jour.year + 1)
    except ValueError:  # 29 février
        return jour.replace(year=jour.year + 1, day=28)


class ClotureMixin:
    """Clôture et affectation de ``Comptabilite``."""

    def cloturer_exercice(self, code, *, session: Session | None = None) -> dict:
        def _t(s: Session) -> dict:
            ex = self._exercice_par_code(s, str(code))
            if ex is None:
                raise ExerciceClos(f"Exercice {code!r} introuvable.")
            if ex.statut == Exercice.CLOTURE:
                raise ExerciceClos(f"L'exercice {ex.code} est déjà clôturé.")
            precedent = self._exercice_precedent(s, ex)
            if precedent is not None and precedent.statut != Exercice.CLOTURE:
                raise ExerciceClos(
                    f"Clôturez d'abord l'exercice précédent ({precedent.code}).")
            brouillards = s.scalar(select(func.count(Ecriture.id)).where(
                Ecriture.tenant_id == self.tenant_id,
                Ecriture.exercice_id == ex.id,
                Ecriture.statut == Ecriture.BROUILLARD))
            if brouillards:
                raise ExerciceClos(
                    f"{brouillards} écriture(s) en brouillard dans l'exercice : "
                    "validez-les ou supprimez-les avant la clôture.")
            integrite = self.verifier_integrite(session=s)
            if not integrite["ok"]:
                raise ErreurComptable(
                    "Chaîne d'intégrité rompue : clôture refusée. Anomalies : "
                    + "; ".join(f"{a['numero']} ({a['anomalie']})"
                                for a in integrite["anomalies"][:5]))

            # 2. Détermination du résultat.
            mouvements = self._agreger(s, ex, exclure_types=(Ecriture.A_NOUVEAU,))
            lignes = []
            resultat = ZERO   # positif = bénéfice
            for numero in sorted(mouvements):
                d, c = mouvements[numero]
                if not numero.startswith(CLASSES_GESTION) or d == c:
                    continue
                solde = d - c
                resultat -= solde
                lignes.append(Ligne(compte=numero, libelle="Solde de clôture",
                                    **({"credit": solde} if solde > 0
                                       else {"debit": -solde})))
            ecriture_resultat = None
            if lignes:
                if resultat > 0:
                    lignes.append(Ligne(
                        compte=self._parametre(s, "compte_resultat_benefice"),
                        credit=resultat, libelle="Résultat net : bénéfice"))
                elif resultat < 0:
                    lignes.append(Ligne(
                        compte=self._parametre(s, "compte_resultat_perte"),
                        debit=-resultat, libelle="Résultat net : perte"))
                ecriture_resultat = self._saisir(
                    self._parametre(s, "journal_od"), ex.date_fin,
                    f"Détermination du résultat {ex.code}", lignes,
                    reference=None, valider=True, source_type="cloture",
                    source_id=f"resultat:{ex.code}",
                    type_ecriture=Ecriture.CLOTURE, session=s)

            # 3. Exercice suivant et à-nouveaux.
            suivant = s.scalar(select(Exercice).where(
                Exercice.tenant_id == self.tenant_id,
                Exercice.date_debut == ex.date_fin + timedelta(days=1)))
            if suivant is None:
                debut = ex.date_fin + timedelta(days=1)
                fin = _meme_jour_annee_suivante(ex.date_fin)
                code_suivant = (str(fin.year) if (debut.month, debut.day) == (1, 1)
                                else f"{debut.year}-{fin.year}")
                suivant = self._creer_exercice(s, code_suivant, debut, fin)
            if self._a_des_a_nouveaux(s, suivant):
                raise ExerciceClos(
                    f"L'exercice {suivant.code} a déjà ses à-nouveaux.")
            ecriture_an = self._passer_a_nouveaux(s, ex, suivant)

            # 4. Statut et verrouillage.
            ex.statut = Exercice.CLOTURE
            ex.cloture_le = utc_now()
            ex.cloture_par = self.utilisateur
            ex.is_synced = False
            verrou = self._date_verrouillage(s)
            if verrou is None or verrou < ex.date_fin:
                self._ecrire_parametre(s, "date_verrouillage", ex.date_fin.isoformat())
            s.flush()
            return {"exercice": ex.code, "resultat": resultat,
                    "ecriture_resultat": ecriture_resultat,
                    "exercice_suivant": suivant.code, "a_nouveaux": ecriture_an}

        return self._executer_avec_reprise_numerotation(_t, session)

    def _passer_a_nouveaux(self, s: Session, ex: Exercice, suivant: Exercice
                           ) -> dict | None:
        lettrables = set(s.scalars(select(Compte.numero).where(
            Compte.tenant_id == self.tenant_id, Compte.lettrable.is_(True))))
        # Soldes de bilan par compte (ouverture + exercice, clôture incluse).
        soldes = self._soldes_a_date(s, ex, ex.date_fin)
        # Détail par tiers pour les comptes lettrables : ouverture (AN de N)
        # + mouvements de N, ventilés par tiers.
        par_tiers: dict[tuple[str, str | None], list] = {}
        req = (select(LigneEcriture.compte_numero, LigneEcriture.tiers_ref,
                      func.max(LigneEcriture.tiers_nom),
                      func.coalesce(func.sum(LigneEcriture.debit), 0),
                      func.coalesce(func.sum(LigneEcriture.credit), 0))
               .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
               .where(LigneEcriture.tenant_id == self.tenant_id,
                      LigneEcriture.deleted_at.is_(None),
                      Ecriture.statut == Ecriture.VALIDEE,
                      Ecriture.exercice_id == ex.id,
                      LigneEcriture.compte_numero.in_(lettrables))
               .group_by(LigneEcriture.compte_numero, LigneEcriture.tiers_ref))
        from .etats import _dec
        for numero, ref, nom, d, c in s.execute(req):
            par_tiers[(numero, ref)] = [nom, _dec(d) - _dec(c)]
        # Échéance la plus ancienne encore ouverte, par tiers : la balance
        # âgée de N+1 garde la vraie ancienneté de la créance.
        lettrees = select(LigneLettrage.ligne_id).where(
            LigneLettrage.tenant_id == self.tenant_id,
            LigneLettrage.deleted_at.is_(None))
        echeances = {(n, ref): ech for n, ref, ech in s.execute(
            select(LigneEcriture.compte_numero, LigneEcriture.tiers_ref,
                   func.min(func.coalesce(LigneEcriture.date_echeance,
                                          Ecriture.date_ecriture)))
            .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
            .where(LigneEcriture.tenant_id == self.tenant_id,
                   LigneEcriture.deleted_at.is_(None),
                   Ecriture.statut == Ecriture.VALIDEE,
                   Ecriture.exercice_id == ex.id,
                   LigneEcriture.compte_numero.in_(lettrables),
                   LigneEcriture.debit > 0,
                   LigneEcriture.id.not_in(lettrees))
            .group_by(LigneEcriture.compte_numero, LigneEcriture.tiers_ref))}
        a_nouveaux_ex = self._a_des_a_nouveaux(s, ex)
        lignes = []
        for numero in sorted(soldes):
            solde = soldes[numero]
            if numero.startswith(CLASSES_GESTION) or not solde:
                continue
            detail = [(ref, nom, v) for (n, ref), (nom, v) in par_tiers.items()
                      if n == numero and v]
            total_detail = sum((v for _, _, v in detail), ZERO)
            # Sans à-nouveaux en N (premier exercice, ou ouverture virtuelle),
            # le solde d'ouverture n'a pas de détail par tiers : le reliquat
            # éventuel est reporté sans tiers.
            if numero in lettrables and detail:
                for ref, nom, v in sorted(detail, key=lambda x: (x[0] or "")):
                    ech = echeances.get((numero, ref)) if v > 0 else None
                    if isinstance(ech, str):
                        ech = date.fromisoformat(ech[:10])
                    lignes.append(Ligne(compte=numero, tiers_ref=ref, tiers_nom=nom,
                                        libelle="À-nouveau", date_echeance=ech,
                                        **({"debit": v} if v > 0 else {"credit": -v})))
                reliquat = solde - total_detail
                if reliquat and not a_nouveaux_ex:
                    lignes.append(Ligne(compte=numero, libelle="À-nouveau",
                                        **({"debit": reliquat} if reliquat > 0
                                           else {"credit": -reliquat})))
                elif reliquat:
                    raise ErreurComptable(
                        f"Écart de ventilation par tiers sur {numero} ({reliquat}).")
                continue
            lignes.append(Ligne(compte=numero, libelle="À-nouveau",
                                **({"debit": solde} if solde > 0
                                   else {"credit": -solde})))
        if len(lignes) < 2:
            return None
        return self._saisir(self._parametre(s, "journal_a_nouveaux"),
                            suivant.date_debut, f"À-nouveaux {suivant.code}",
                            lignes, reference=None, valider=True,
                            source_type="a_nouveaux",
                            source_id=f"an:{suivant.code}",
                            type_ecriture=Ecriture.A_NOUVEAU, session=s)

    def affecter_resultat(self, code_exercice, date_affectation, *,
                          reserve_legale=0, autres_reserves=0, dividendes=0,
                          session: Session | None = None) -> dict:
        """Affectation du résultat d'un exercice clôturé (décision de l'AGO).

        Bénéfice : réserve légale (111), autres réserves (118), dividendes
        (465), le reste en report à nouveau créditeur (121).
        Perte : intégralement en report à nouveau débiteur (129)."""
        jour = _date(date_affectation)
        rl = montant(reserve_legale, champ="réserve légale")
        ar = montant(autres_reserves, champ="autres réserves")
        dv = montant(dividendes, champ="dividendes")
        if min(rl, ar, dv) < ZERO:
            raise EcritureInvalide("Les montants affectés sont positifs.")

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code(s, str(code_exercice))
            if ex is None or ex.statut != Exercice.CLOTURE:
                raise ExerciceClos("L'affectation porte sur un exercice clôturé.")
            suivant = self._exercice_pour(s, jour)
            if suivant.date_debut <= ex.date_fin:
                raise EcritureInvalide("L'affectation se date dans l'exercice suivant.")
            soldes = self._soldes_ouverture(s, suivant)
            benefice_cpt = self._parametre(s, "compte_resultat_benefice")
            perte_cpt = self._parametre(s, "compte_resultat_perte")
            benefice = -soldes.get(benefice_cpt, ZERO)
            perte = soldes.get(perte_cpt, ZERO)
            lignes = []
            if benefice > 0:
                reparti = rl + ar + dv
                if reparti > benefice:
                    raise EcritureInvalide(
                        f"Affectation ({reparti}) supérieure au bénéfice ({benefice}).")
                lignes.append(Ligne(compte=benefice_cpt, debit=benefice,
                                    libelle="Affectation du bénéfice"))
                for cpt, m, lib in (("111", rl, "Réserve légale"),
                                    ("118", ar, "Autres réserves"),
                                    ("465", dv, "Dividendes à payer"),
                                    (self._parametre(s, "compte_report_crediteur"),
                                     benefice - reparti, "Report à nouveau")):
                    if m:
                        lignes.append(Ligne(compte=cpt, credit=m, libelle=lib))
            elif perte > 0:
                if rl or ar or dv:
                    raise EcritureInvalide("Une perte ne se distribue pas.")
                lignes = [Ligne(compte=self._parametre(s, "compte_report_debiteur"),
                                debit=perte, libelle="Report à nouveau débiteur"),
                          Ligne(compte=perte_cpt, credit=perte,
                                libelle="Affectation de la perte")]
            else:
                raise EcritureInvalide("Aucun résultat à affecter.")
            return self._saisir(self._parametre(s, "journal_od"), jour,
                                f"Affectation du résultat {ex.code}", lignes,
                                reference=None, valider=True,
                                source_type="affectation", source_id=ex.code,
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)
