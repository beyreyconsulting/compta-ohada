"""
États comptables : balance générale, grand livre, journaux, balance des
tiers, balance âgée, déclaration et liquidation de TVA.

Règles communes :

- seules les écritures VALIDÉES comptent (un brouillard n'est pas comptable) ;
- une période se lit à l'intérieur d'UN exercice ;
- les soldes d'ouverture d'un exercice sont ses à-nouveaux (journal AN). Si
  l'exercice précédent n'est pas encore clôturé, ils sont reconstitués
  « à blanc » depuis ses soldes de clôture, son résultat étant porté en 130
  (résultat en instance d'affectation) — les états restent justes et
  équilibrés avant même la clôture définitive.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .erreurs import EcritureInvalide, ExerciceIntrouvable
from .modeles import Compte, Ecriture, Exercice, LigneEcriture, LigneLettrage, Taxe
from .noyau import Ligne, _date
from .socle import ZERO

COMPTE_RESULTAT_EN_INSTANCE = "130"
CLASSES_BILAN = ("1", "2", "3", "4", "5")
CLASSES_GESTION = ("6", "7", "8")


def _dec(valeur) -> Decimal:
    """Résultat d'agrégat SQL -> Decimal à 2 décimales."""
    if valeur is None:
        return ZERO
    return Decimal(str(valeur)).quantize(Decimal("0.01"))


def _deux_colonnes(solde: Decimal) -> tuple[Decimal, Decimal]:
    """Solde net -> (débiteur, créditeur)."""
    return (solde, ZERO) if solde >= 0 else (ZERO, -solde)


class EtatsMixin:
    """Méthodes d'états de ``Comptabilite``."""

    # ------------------------------------------------------------------
    # Briques de calcul
    # ------------------------------------------------------------------

    def _exercice_couvrant(self, s: Session, debut: date, fin: date) -> Exercice:
        ex = self._exercice_pour(s, debut)
        if fin < debut:
            raise EcritureInvalide("La fin de période précède son début.")
        if fin > ex.date_fin:
            raise ExerciceIntrouvable(
                f"La période déborde l'exercice {ex.code} (fin le "
                f"{ex.date_fin:%d/%m/%Y}) : un état se lit dans un seul exercice.")
        return ex

    def _agreger(self, s: Session, ex: Exercice, *, debut: date | None = None,
                 fin: date | None = None, types: tuple[str, ...] | None = None,
                 exclure_types: tuple[str, ...] = ()
                 ) -> dict[str, tuple[Decimal, Decimal]]:
        """Σ débit, Σ crédit par compte pour les écritures validées de l'exercice."""
        req = (select(LigneEcriture.compte_numero,
                      func.coalesce(func.sum(LigneEcriture.debit), 0),
                      func.coalesce(func.sum(LigneEcriture.credit), 0))
               .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
               .where(LigneEcriture.tenant_id == self.tenant_id,
                      LigneEcriture.deleted_at.is_(None),
                      Ecriture.deleted_at.is_(None),
                      Ecriture.statut == Ecriture.VALIDEE,
                      Ecriture.exercice_id == ex.id)
               .group_by(LigneEcriture.compte_numero))
        if debut is not None:
            req = req.where(Ecriture.date_ecriture >= debut)
        if fin is not None:
            req = req.where(Ecriture.date_ecriture <= fin)
        if types:
            req = req.where(Ecriture.type_ecriture.in_(types))
        if exclure_types:
            req = req.where(Ecriture.type_ecriture.not_in(exclure_types))
        resultat = {}
        for numero, d, c in s.execute(req):
            # SUM d'une colonne Montant : le TypeDecorator reconvertit déjà
            # les centimes en unités (Decimal à 2 décimales).
            resultat[numero] = (_dec(d), _dec(c))
        return resultat

    def _exercice_precedent(self, s: Session, ex: Exercice) -> Exercice | None:
        return s.scalar(select(Exercice).where(
            Exercice.tenant_id == self.tenant_id,
            Exercice.date_fin < ex.date_debut)
            .order_by(Exercice.date_fin.desc()).limit(1))

    def _a_des_a_nouveaux(self, s: Session, ex: Exercice) -> bool:
        return bool(s.scalar(select(func.count(Ecriture.id)).where(
            Ecriture.tenant_id == self.tenant_id,
            Ecriture.exercice_id == ex.id,
            Ecriture.statut == Ecriture.VALIDEE,
            Ecriture.type_ecriture == Ecriture.A_NOUVEAU)))

    def _soldes_ouverture(self, s: Session, ex: Exercice, _profondeur: int = 0
                          ) -> dict[str, Decimal]:
        """Soldes nets (débit − crédit) d'ouverture de l'exercice."""
        if _profondeur > 50:
            raise ExerciceIntrouvable("Chaîne d'exercices trop longue (> 50).")
        if self._a_des_a_nouveaux(s, ex):
            return {n: d - c for n, (d, c) in self._agreger(
                s, ex, types=(Ecriture.A_NOUVEAU,)).items() if d != c}
        precedent = self._exercice_precedent(s, ex)
        if precedent is None:
            return {}
        return self._soldes_cloture_virtuelle(s, precedent, _profondeur + 1)

    def _soldes_cloture_virtuelle(self, s: Session, ex: Exercice,
                                  _profondeur: int = 0) -> dict[str, Decimal]:
        """Soldes de bilan en fin d'exercice, résultat porté en 130 s'il n'a
        pas été viré par une écriture de clôture."""
        soldes = dict(self._soldes_ouverture(s, ex, _profondeur))
        for n, (d, c) in self._agreger(
                s, ex, exclure_types=(Ecriture.A_NOUVEAU,)).items():
            soldes[n] = soldes.get(n, ZERO) + d - c
        resultat = ZERO
        bilan: dict[str, Decimal] = {}
        for n, solde in soldes.items():
            if n.startswith(CLASSES_GESTION):
                resultat += solde
            elif n.startswith(CLASSES_BILAN):
                bilan[n] = solde
        if resultat:
            bilan[COMPTE_RESULTAT_EN_INSTANCE] = (
                bilan.get(COMPTE_RESULTAT_EN_INSTANCE, ZERO) + resultat)
        return {n: v for n, v in bilan.items() if v}

    def _soldes_a_date(self, s: Session, ex: Exercice, fin: date, *,
                       exclure_cloture: bool = False) -> dict[str, Decimal]:
        soldes = dict(self._soldes_ouverture(s, ex))
        exclus = (Ecriture.A_NOUVEAU,) + ((Ecriture.CLOTURE,) if exclure_cloture else ())
        for n, (d, c) in self._agreger(s, ex, fin=fin, exclure_types=exclus).items():
            soldes[n] = soldes.get(n, ZERO) + d - c
        return soldes

    def _libelles(self, s: Session) -> dict[str, str]:
        return dict(s.execute(select(Compte.numero, Compte.libelle).where(
            Compte.tenant_id == self.tenant_id)).all())

    # ------------------------------------------------------------------
    # Balance générale
    # ------------------------------------------------------------------

    def balance(self, debut, fin, *, classes: tuple[int, ...] | None = None,
                niveau: int | None = None,
                session: Session | None = None) -> dict:
        """Balance générale à six colonnes sur une période d'un exercice.

        ``niveau`` agrège par préfixe (2 = rubriques, 3 = comptes principaux).
        Renvoie ``{"lignes": [...], "totaux": {...}, "equilibree": bool}``."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> dict:
            ex = self._exercice_couvrant(s, debut, fin)
            ouverture = dict(self._soldes_ouverture(s, ex))
            avant = self._agreger_strict_avant(s, ex, debut)
            for n, (d, c) in avant.items():
                ouverture[n] = ouverture.get(n, ZERO) + d - c
            mouvements = self._agreger(s, ex, debut=debut, fin=fin,
                                       exclure_types=(Ecriture.A_NOUVEAU,))
            numeros = set(ouverture) | set(mouvements)
            libelles = self._libelles(s)
            lignes: dict[str, dict] = {}
            for n in numeros:
                if classes and int(n[0]) not in classes:
                    continue
                cle = n[:niveau] if niveau else n
                o = ouverture.get(n, ZERO)
                md, mc = mouvements.get(n, (ZERO, ZERO))
                l = lignes.setdefault(cle, {"compte": cle,
                                            "libelle": libelles.get(cle, ""),
                                            "ouverture": ZERO, "debit": ZERO,
                                            "credit": ZERO})
                l["ouverture"] += o
                l["debit"] += md
                l["credit"] += mc
            resultat = []
            totaux = dict.fromkeys(("ouverture_debit", "ouverture_credit", "debit",
                                    "credit", "solde_debit", "solde_credit"), ZERO)
            for cle in sorted(lignes):
                l = lignes[cle]
                od, oc = _deux_colonnes(l["ouverture"])
                sd, sc = _deux_colonnes(l["ouverture"] + l["debit"] - l["credit"])
                if not any((od, oc, l["debit"], l["credit"], sd, sc)):
                    continue
                ligne = {"compte": cle, "libelle": l["libelle"],
                         "ouverture_debit": od, "ouverture_credit": oc,
                         "debit": l["debit"], "credit": l["credit"],
                         "solde_debit": sd, "solde_credit": sc}
                resultat.append(ligne)
                for k in totaux:
                    totaux[k] += ligne[k]
            return {"exercice": ex.code, "debut": debut, "fin": fin,
                    "lignes": resultat, "totaux": totaux,
                    "equilibree": (totaux["debit"] == totaux["credit"]
                                   and totaux["solde_debit"] == totaux["solde_credit"]
                                   and totaux["ouverture_debit"] == totaux["ouverture_credit"])}
        return self._lire(_t, session)

    def _agreger_strict_avant(self, s: Session, ex: Exercice, jour: date
                              ) -> dict[str, tuple[Decimal, Decimal]]:
        """Mouvements de l'exercice STRICTEMENT antérieurs à ``jour`` (hors AN)."""
        veille = date.fromordinal(jour.toordinal() - 1)
        if veille < ex.date_debut:
            return {}
        return self._agreger(s, ex, fin=veille, exclure_types=(Ecriture.A_NOUVEAU,))

    # ------------------------------------------------------------------
    # Grand livre
    # ------------------------------------------------------------------

    def grand_livre(self, debut, fin, *, comptes: list[str] | None = None,
                    prefixe: str | None = None, tiers_ref: str | None = None,
                    session: Session | None = None) -> list[dict]:
        """Grand livre : solde d'ouverture, chaque mouvement, solde progressif."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> list[dict]:
            ex = self._exercice_couvrant(s, debut, fin)
            ouverture = dict(self._soldes_ouverture(s, ex)) if tiers_ref is None else {}
            if tiers_ref is None:
                for n, (d, c) in self._agreger_strict_avant(s, ex, debut).items():
                    ouverture[n] = ouverture.get(n, ZERO) + d - c
            req = (select(LigneEcriture, Ecriture)
                   .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                   .where(LigneEcriture.tenant_id == self.tenant_id,
                          LigneEcriture.deleted_at.is_(None),
                          Ecriture.statut == Ecriture.VALIDEE,
                          Ecriture.exercice_id == ex.id,
                          Ecriture.date_ecriture >= debut,
                          Ecriture.date_ecriture <= fin,
                          Ecriture.type_ecriture != Ecriture.A_NOUVEAU)
                   .order_by(LigneEcriture.compte_numero, Ecriture.date_ecriture,
                             Ecriture.journal_code, Ecriture.poste,
                             Ecriture.sequence, LigneEcriture.ordre))
            if comptes:
                req = req.where(LigneEcriture.compte_numero.in_([str(c) for c in comptes]))
            if prefixe:
                req = req.where(LigneEcriture.compte_numero.startswith(str(prefixe)))
            if tiers_ref is not None:
                req = req.where(LigneEcriture.tiers_ref == tiers_ref)
            par_compte: dict[str, list] = {}
            for lg, e in s.execute(req):
                par_compte.setdefault(lg.compte_numero, []).append((lg, e))
            numeros = set(par_compte)
            if tiers_ref is None:
                numeros |= {n for n, v in ouverture.items() if v and (
                    (not comptes or n in comptes) and
                    (not prefixe or n.startswith(prefixe)))}
            libelles = self._libelles(s)
            livre = []
            for n in sorted(numeros):
                solde = ouverture.get(n, ZERO)
                mouvements = []
                td = tc = ZERO
                for lg, e in par_compte.get(n, []):
                    solde += lg.debit - lg.credit
                    td += lg.debit
                    tc += lg.credit
                    mouvements.append({
                        "date": e.date_ecriture, "journal": e.journal_code,
                        "numero": e.numero, "reference": e.reference,
                        "libelle": lg.libelle, "tiers_ref": lg.tiers_ref,
                        "debit": lg.debit, "credit": lg.credit, "solde": solde})
                livre.append({"compte": n, "libelle": libelles.get(n, ""),
                              "ouverture": ouverture.get(n, ZERO),
                              "mouvements": mouvements, "total_debit": td,
                              "total_credit": tc, "solde": solde})
            return livre
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    def journal(self, code: str, debut, fin, *,
                session: Session | None = None) -> dict:
        """Journal : écritures chronologiques et totaux (doivent être égaux)."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")
        ecritures = self.ecritures(debut=debut, fin=fin, journal=code,
                                   session=session)
        td = sum((lg["debit"] for e in ecritures for lg in e["lignes"]), ZERO)
        tc = sum((lg["credit"] for e in ecritures for lg in e["lignes"]), ZERO)
        return {"journal": code.upper(), "debut": debut, "fin": fin,
                "ecritures": ecritures, "total_debit": td, "total_credit": tc,
                "equilibre": td == tc}

    def journal_centralisateur(self, debut, fin, *,
                               session: Session | None = None) -> list[dict]:
        """Totaux débit/crédit par journal et par mois."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> list[dict]:
            self._exercice_couvrant(s, debut, fin)
            req = (select(Ecriture.journal_code, Ecriture.date_ecriture,
                          LigneEcriture.debit, LigneEcriture.credit)
                   .join(LigneEcriture, LigneEcriture.ecriture_id == Ecriture.id)
                   .where(Ecriture.tenant_id == self.tenant_id,
                          Ecriture.statut == Ecriture.VALIDEE,
                          LigneEcriture.deleted_at.is_(None),
                          Ecriture.date_ecriture >= debut,
                          Ecriture.date_ecriture <= fin))
            agreg: dict[tuple[str, str], list[Decimal]] = {}
            for code, jour, d, c in s.execute(req):
                cle = (code, f"{jour:%Y-%m}")
                t = agreg.setdefault(cle, [ZERO, ZERO])
                t[0] += d
                t[1] += c
            return [{"journal": j, "mois": m, "debit": d, "credit": c}
                    for (j, m), (d, c) in sorted(agreg.items())]
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Tiers
    # ------------------------------------------------------------------

    def balance_tiers(self, prefixe: str, fin, *,
                      session: Session | None = None) -> list[dict]:
        """Solde par tiers des comptes commençant par ``prefixe`` (411, 401,
        422…) à la date ``fin``, depuis le début de l'exercice (à-nouveaux
        inclus). Trié du plus gros solde au plus petit."""
        fin = _date(fin, champ="fin")

        def _t(s: Session) -> list[dict]:
            ex = self._exercice_pour(s, fin)
            req = (select(LigneEcriture.tiers_ref,
                          func.max(LigneEcriture.tiers_nom),
                          func.coalesce(func.sum(LigneEcriture.debit), 0),
                          func.coalesce(func.sum(LigneEcriture.credit), 0))
                   .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                   .where(LigneEcriture.tenant_id == self.tenant_id,
                          LigneEcriture.deleted_at.is_(None),
                          LigneEcriture.compte_numero.startswith(str(prefixe)),
                          Ecriture.statut == Ecriture.VALIDEE,
                          Ecriture.exercice_id == ex.id,
                          Ecriture.date_ecriture <= fin)
                   .group_by(LigneEcriture.tiers_ref))
            lignes = []
            for ref, nom, d, c in s.execute(req):
                d, c = _dec(d), _dec(c)
                if d == c:
                    continue
                lignes.append({"tiers_ref": ref, "tiers_nom": nom, "debit": d,
                               "credit": c, "solde": d - c})
            return sorted(lignes, key=lambda x: abs(x["solde"]), reverse=True)
        return self._lire(_t, session)

    def balance_agee(self, prefixe: str, jour, *,
                     tranches: tuple[int, ...] = (30, 60, 90),
                     session: Session | None = None) -> list[dict]:
        """Balance âgée des créances (411) ou dettes (401) non lettrées.

        Les règlements non lettrés soldent d'abord les pièces les plus
        anciennes (FIFO) ; l'ancienneté se compte depuis l'échéance, à
        défaut depuis la date de la pièce."""
        jour = _date(jour)

        def _t(s: Session) -> list[dict]:
            ex = self._exercice_pour(s, jour)
            lettrees = select(LigneLettrage.ligne_id).where(
                LigneLettrage.tenant_id == self.tenant_id,
                LigneLettrage.deleted_at.is_(None))
            req = (select(LigneEcriture, Ecriture.date_ecriture)
                   .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                   .where(LigneEcriture.tenant_id == self.tenant_id,
                          LigneEcriture.deleted_at.is_(None),
                          Ecriture.exercice_id == ex.id,
                          LigneEcriture.compte_numero.startswith(str(prefixe)),
                          Ecriture.statut == Ecriture.VALIDEE,
                          Ecriture.date_ecriture <= jour,
                          LigneEcriture.id.not_in(lettrees))
                   .order_by(Ecriture.date_ecriture, LigneEcriture.id))
            par_tiers: dict = {}
            for lg, date_piece in s.execute(req):
                par_tiers.setdefault((lg.compte_numero, lg.tiers_ref), []).append(
                    (lg, date_piece))
            noms_tranches = ["non_echu"] + [
                f"{(tranches[i - 1] + 1) if i else 0}_{t}" for i, t in enumerate(tranches)
            ] + [f"plus_{tranches[-1]}"]
            resultat = []
            for (compte, ref), lignes in par_tiers.items():
                solde_total = sum((lg.solde for lg, _ in lignes), ZERO)
                if solde_total == ZERO:
                    continue
                sens = 1 if solde_total > 0 else -1
                pieces = [[lg, d, sens * lg.solde] for lg, d in lignes
                          if sens * lg.solde > 0]
                contre = sum((-sens * lg.solde for lg, _ in lignes
                              if sens * lg.solde < 0), ZERO)
                for p in pieces:
                    impute = min(p[2], contre)
                    p[2] -= impute
                    contre -= impute
                ligne = {"compte": compte, "tiers_ref": ref,
                         "tiers_nom": next((lg.tiers_nom for lg, _ in lignes
                                            if lg.tiers_nom), None),
                         "solde": solde_total,
                         **{n: ZERO for n in noms_tranches}}
                for lg, d, reste in pieces:
                    if reste <= 0:
                        continue
                    echeance = lg.date_echeance or d
                    retard = (jour - echeance).days
                    if retard <= 0:
                        nom = "non_echu"
                    else:
                        nom = noms_tranches[-1]
                        for i, t in enumerate(tranches):
                            if retard <= t:
                                nom = noms_tranches[i + 1]
                                break
                    ligne[nom] += sens * reste
                resultat.append(ligne)
            return sorted(resultat, key=lambda x: abs(x["solde"]), reverse=True)
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # TVA
    # ------------------------------------------------------------------

    def declaration_tva(self, debut, fin, *,
                        session: Session | None = None) -> dict:
        """Éléments de la déclaration de TVA de la période.

        Montants de TVA lus sur les COMPTES (443x collectée, 445x déductible)
        — y compris les écritures manuelles — ; bases et détail par taux lus
        sur les lignes marquées d'un code de taxe. Les écritures de
        liquidation de TVA sont exclues."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> dict:
            ex = self._exercice_couvrant(s, debut, fin)
            base_req = (select(LigneEcriture)
                        .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                        .where(LigneEcriture.tenant_id == self.tenant_id,
                               LigneEcriture.deleted_at.is_(None),
                               Ecriture.statut == Ecriture.VALIDEE,
                               Ecriture.exercice_id == ex.id,
                               Ecriture.date_ecriture >= debut,
                               Ecriture.date_ecriture <= fin,
                               Ecriture.type_ecriture.not_in(
                                   (Ecriture.A_NOUVEAU, Ecriture.CLOTURE)),
                               (Ecriture.source_type.is_(None))
                               | (Ecriture.source_type != "tva.liquidation")))
            taxes = {t.code: t for t in s.scalars(select(Taxe).where(
                Taxe.tenant_id == self.tenant_id))}
            detail: dict[str, dict] = {}
            collectee = deductible = ZERO
            for lg in s.scalars(base_req):
                n = lg.compte_numero
                if n.startswith("443"):
                    collectee += lg.credit - lg.debit
                elif n.startswith("445"):
                    deductible += lg.debit - lg.credit
                if not lg.taxe_code:
                    continue
                taxe = taxes.get(lg.taxe_code)
                if taxe is None:
                    continue
                signe = 1 if taxe.type_taxe == Taxe.VENTE else -1
                d = detail.setdefault(lg.taxe_code, {
                    "code": taxe.code, "libelle": taxe.libelle, "taux": taxe.taux,
                    "type": taxe.type_taxe, "base": ZERO, "tva": ZERO})
                net = (lg.credit - lg.debit) * signe
                if lg.base_taxe is None:
                    d["base"] += net
                else:
                    d["tva"] += net
            credit_anterieur = ZERO
            soldes = dict(self._soldes_ouverture(s, ex))
            for n, (dd, cc) in self._agreger_strict_avant(s, ex, debut).items():
                soldes[n] = soldes.get(n, ZERO) + dd - cc
            compte_credit = self._parametre(s, "compte_credit_tva")
            credit_anterieur = max(soldes.get(compte_credit, ZERO), ZERO)
            net = collectee - deductible - credit_anterieur
            return {
                "debut": debut, "fin": fin,
                "tva_collectee": collectee, "tva_deductible": deductible,
                "credit_anterieur": credit_anterieur,
                "tva_due": net if net > 0 else ZERO,
                "credit_a_reporter": -net if net < 0 else ZERO,
                "detail": sorted(detail.values(), key=lambda x: (x["type"], x["code"])),
            }
        return self._lire(_t, session)

    def liquider_tva(self, debut, fin, *, date_liquidation=None,
                     session: Session | None = None) -> dict | None:
        """Solde les comptes de TVA de la période vers 4441 (TVA due) ou 4449
        (crédit à reporter). Idempotent par période."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")
        jour = _date(date_liquidation) if date_liquidation else fin

        def _t(s: Session) -> dict | None:
            ex = self._exercice_couvrant(s, debut, fin)
            source_id = f"{debut.isoformat()}:{fin.isoformat()}"
            existante = self._ecriture_source(s, "tva.liquidation", source_id)
            if existante is not None:
                from .noyau import resume_ecriture
                return resume_ecriture(existante, existante.lignes)
            mouvements = {}
            for n, (d, c) in self._agreger(
                    s, ex, debut=debut, fin=fin,
                    exclure_types=(Ecriture.A_NOUVEAU, Ecriture.CLOTURE)).items():
                if n.startswith(("443", "445")) and d != c:
                    mouvements[n] = d - c
            decl = self.declaration_tva(debut, fin, session=s)
            lignes = []
            for n, solde in sorted(mouvements.items()):
                lignes.append(Ligne(compte=n, libelle="Liquidation de TVA",
                                    **({"credit": solde} if solde > 0
                                       else {"debit": -solde})))
            compte_credit = self._parametre(s, "compte_credit_tva")
            if decl["credit_anterieur"]:
                lignes.append(Ligne(compte=compte_credit,
                                    credit=decl["credit_anterieur"],
                                    libelle="Imputation du crédit de TVA antérieur"))
            if decl["tva_due"]:
                lignes.append(Ligne(compte=self._parametre(s, "compte_tva_due"),
                                    credit=decl["tva_due"], libelle="TVA due"))
            if decl["credit_a_reporter"]:
                lignes.append(Ligne(compte=compte_credit,
                                    debit=decl["credit_a_reporter"],
                                    libelle="Crédit de TVA à reporter"))
            if len(lignes) < 2:
                return None
            return self._saisir(self._parametre(s, "journal_od"), jour,
                                f"Liquidation TVA {debut:%d/%m/%Y}–{fin:%d/%m/%Y}",
                                lignes, reference=None, valider=True,
                                source_type="tva.liquidation", source_id=source_id,
                                type_ecriture=Ecriture.NORMALE, session=s)
        return self._executer_avec_reprise_numerotation(_t, session)
