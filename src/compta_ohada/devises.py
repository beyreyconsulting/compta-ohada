"""
Opérations en devises (SYSCOHADA révisé).

Principes :

- la comptabilité est tenue dans la monnaie du tenant (XOF par défaut) ; une
  opération en devise est convertie au cours du jour de l'opération, et ses
  lignes conservent le montant en devise ;
- au règlement, l'écart entre le cours historique et le cours du jour est un
  écart de change RÉALISÉ : perte 656 / gain 756 (créances et dettes
  commerciales) ;
- à la clôture, les créances et dettes en devise encore ouvertes sont
  converties au cours de clôture ; l'écart est LATENT : écart de conversion
  actif 478 (perte latente) ou passif 479 (gain latent), et une provision pour
  perte de change (194, dotation 697 / reprise 797) couvre les pertes
  latentes. L'écriture d'écarts est contre-passée à l'ouverture de
  l'exercice suivant ;
- les comptes de trésorerie tenus en devise (``creer_compte(..., devise=…)``)
  sont convertis au cours de clôture, l'écart allant directement en résultat
  financier (676 / 776).

Le franc CFA ayant une parité fixe avec l'euro (1 EUR = 655,957 XOF), ce cours
est installé d'office ; les autres devises (USD, GNF, NGN…) se saisissent
avec ``definir_cours``.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .erreurs import ParametreInvalide
from .modeles import (Compte, Cours, Ecriture, LigneEcriture, LigneLettrage,
                      id_deterministe)
from .noyau import (Ligne, _date, resume_ecriture,
                    valider_code_devise)
from .socle import CENTIME, INCLURE_SUPPRIMES, ZERO, montant

def convertir(montant_devise: Decimal, cours: Decimal) -> Decimal:
    return (montant_devise * cours).quantize(CENTIME, rounding=ROUND_HALF_UP)


class DevisesMixin:
    """Cours, conversions, écarts de change et réévaluation de clôture."""

    # ------------------------------------------------------------------
    # Cours
    # ------------------------------------------------------------------

    def definir_cours(self, devise: str, jour, cours, *,
                      session: Session | None = None) -> dict:
        """Enregistre le cours d'une devise à une date (valeur d'une unité en
        monnaie de tenue). Remplace un cours déjà saisi pour ce jour."""
        devise = valider_code_devise(devise)
        jour = _date(jour)
        valeur = Decimal(str(cours))
        if not valeur.is_finite() or valeur <= 0:
            raise ParametreInvalide("Un cours est strictement positif.")
        micro = int((valeur * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))

        def _t(s: Session) -> dict:
            if devise == (self._parametre(s, "devise") or "XOF"):
                raise ParametreInvalide(f"{devise} est la monnaie de tenue.")
            cid = id_deterministe(self.tenant_id, "cours", f"{devise}|{jour.isoformat()}")
            c = s.get(Cours, cid, execution_options=INCLURE_SUPPRIMES)
            if c is None:
                c = Cours(id=cid, tenant_id=self.tenant_id, devise=devise,
                          date_cours=jour, cours_micro=micro)
                s.add(c)
            c.cours_micro = micro
            c.deleted_at = None
            c.is_synced = False
            return {"devise": devise, "date": jour, "cours": c.cours}
        return self._executer(_t, session)

    def _cours(self, s: Session, devise: str, jour: date) -> Decimal:
        c = s.scalar(select(Cours).where(
            Cours.tenant_id == self.tenant_id, Cours.devise == devise,
            Cours.date_cours <= jour).order_by(Cours.date_cours.desc()).limit(1))
        if c is None:
            raise ParametreInvalide(
                f"Aucun cours {devise} connu au {jour:%d/%m/%Y} : "
                "saisissez-le avec definir_cours().")
        return c.cours

    # Un cours de clôture plus ancien que ce délai est refusé : réévaluer au
    # 31/12 avec un cours de janvier fausserait le bilan sans alerte.
    AGE_MAX_COURS_CLOTURE = 31

    def _cours_cloture(self, s: Session, devise: str, jour: date,
                       utilises: dict, strict: bool) -> Decimal:
        c = s.scalar(select(Cours).where(
            Cours.tenant_id == self.tenant_id, Cours.devise == devise,
            Cours.date_cours <= jour).order_by(Cours.date_cours.desc()).limit(1))
        if c is None:
            raise ParametreInvalide(
                f"Aucun cours {devise} connu au {jour:%d/%m/%Y} : "
                "saisissez-le avec definir_cours().")
        parite_fixe = devise == "EUR" and (self._parametre(s, "devise") or "XOF") in (
            "XOF", "XAF")
        if (strict and not parite_fixe
                and (jour - c.date_cours).days > self.AGE_MAX_COURS_CLOTURE):
            raise ParametreInvalide(
                f"Le dernier cours {devise} date du {c.date_cours:%d/%m/%Y} : trop "
                f"ancien pour la clôture du {jour:%d/%m/%Y}. Saisissez le cours de "
                f"clôture avec definir_cours(\"{devise}\", \"{jour.isoformat()}\", …).")
        utilises[devise] = {"date": c.date_cours, "cours": c.cours}
        return c.cours

    def cours_du(self, devise: str, jour, *, session: Session | None = None) -> Decimal:
        devise = valider_code_devise(devise)
        jour = _date(jour)
        return self._lire(lambda s: self._cours(s, devise, jour), session)

    # ------------------------------------------------------------------
    # Lignes en devise
    # ------------------------------------------------------------------

    def _convertir_document(self, lignes: list[Ligne], index_tiers: int,
                            devise: str, cours: Decimal) -> list[Ligne]:
        """Lignes exprimées en devise -> lignes en monnaie de tenue.

        Chaque ligne est convertie et arrondie ; la ligne de tiers reçoit la
        somme exacte des autres, pour que l'écriture reste équilibrée."""
        converties: list[Ligne | None] = [None] * len(lignes)
        total_debit = total_credit = ZERO
        for i, lg in enumerate(lignes):
            if i == index_tiers:
                continue
            d = convertir(montant(lg.debit or 0), cours)
            c = convertir(montant(lg.credit or 0), cours)
            m_dev = montant(lg.debit or 0) or montant(lg.credit or 0)
            base = (convertir(montant(lg.base_taxe), cours)
                    if lg.base_taxe is not None else None)
            converties[i] = Ligne(compte=lg.compte, debit=d, credit=c,
                                  libelle=lg.libelle, tiers_ref=lg.tiers_ref,
                                  tiers_nom=lg.tiers_nom,
                                  date_echeance=lg.date_echeance,
                                  taxe_code=lg.taxe_code, base_taxe=base,
                                  devise=devise, montant_devise=m_dev,
                                  section=lg.section)
            total_debit += d
            total_credit += c
        t = lignes[index_tiers]
        m_dev = montant(t.debit or 0) or montant(t.credit or 0)
        ecart = total_credit - total_debit
        converties[index_tiers] = Ligne(
            compte=t.compte, libelle=t.libelle, tiers_ref=t.tiers_ref,
            tiers_nom=t.tiers_nom, date_echeance=t.date_echeance,
            devise=devise, montant_devise=m_dev,
            **({"debit": ecart} if ecart > 0 else {"credit": -ecart}))
        return converties  # type: ignore[return-value]

    def _ouvert_en_devise(self, s: Session, compte: str, tiers_ref: str | None,
                          devise: str, document_id: str | None = None,
                          types: tuple[str, ...] | None = None
                          ) -> tuple[Decimal, Decimal]:
        """(solde en monnaie de tenue, solde en devise) des lignes NON lettrées
        d'un tiers dans une devise, éventuellement limitées à un document."""
        lettrees = select(LigneLettrage.ligne_id).where(
            LigneLettrage.tenant_id == self.tenant_id,
            LigneLettrage.deleted_at.is_(None))
        req = (select(LigneEcriture)
               .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
               .where(LigneEcriture.tenant_id == self.tenant_id,
                      LigneEcriture.deleted_at.is_(None),
                      LigneEcriture.compte_numero == compte,
                      LigneEcriture.devise == devise,
                      Ecriture.statut == Ecriture.VALIDEE,
                      Ecriture.exercice_id.in_(self._exercices_ouverts(s)),
                      LigneEcriture.id.not_in(lettrees)))
        req = req.where(LigneEcriture.tiers_ref == tiers_ref if tiers_ref is not None
                        else LigneEcriture.tiers_ref.is_(None))
        if document_id is not None:
            req = req.where(or_(Ecriture.source_id == document_id,
                                Ecriture.document_lie == document_id))
            if types:
                req = req.where(Ecriture.source_type.in_(types))
        x = d = ZERO
        for lg in s.scalars(req):
            x += lg.debit - lg.credit
            m = lg.montant_devise or ZERO
            d += m if lg.debit else -m
        return x, d

    def _lignes_reglement_devise(self, s: Session, *, m_devise: Decimal,
                                 devise: str, cours: Decimal,
                                 compte_tresorerie: str, compte_tiers: str,
                                 tiers_ref, tiers_nom, encaissement: bool,
                                 document_id: str | None,
                                 types: tuple[str, ...] | None,
                                 libelle: str) -> list[Ligne]:
        xof_tresorerie = convertir(m_devise, cours)
        book_x, book_d = self._ouvert_en_devise(s, compte_tiers, tiers_ref, devise,
                                                document_id, types)
        if document_id is not None and book_d == ZERO:
            book_x, book_d = self._ouvert_en_devise(s, compte_tiers, tiers_ref, devise)
        ouvert_d, ouvert_x = abs(book_d), abs(book_x)
        if ouvert_d == ZERO:
            historique = xof_tresorerie            # avance : pas d'historique
        elif m_devise >= ouvert_d:
            historique = ouvert_x + convertir(m_devise - ouvert_d, cours)
        else:
            historique = (ouvert_x * m_devise / ouvert_d).quantize(
                CENTIME, rounding=ROUND_HALF_UP)
        compte_t = self._compte(s, compte_tresorerie, pour_imputation=True)
        devise_t = {"devise": devise, "montant_devise": m_devise} \
            if compte_t.devise == devise else {}
        lignes = [
            Ligne(compte=compte_tresorerie, libelle=libelle, **devise_t,
                  **({"debit": xof_tresorerie} if encaissement
                     else {"credit": xof_tresorerie})),
            Ligne(compte=compte_tiers, libelle=libelle, tiers_ref=tiers_ref,
                  tiers_nom=tiers_nom, devise=devise, montant_devise=m_devise,
                  **({"credit": historique} if encaissement
                     else {"debit": historique})),
        ]
        ecart = xof_tresorerie - historique
        if not encaissement:
            ecart = -ecart
        if ecart > 0:
            lignes.append(Ligne(compte=self._parametre(s, "compte_gain_change"),
                                credit=ecart, libelle=f"Gain de change — {libelle}"))
        elif ecart < 0:
            lignes.append(Ligne(compte=self._parametre(s, "compte_perte_change"),
                                debit=-ecart, libelle=f"Perte de change — {libelle}"))
        return lignes

    # ------------------------------------------------------------------
    # Réévaluation de clôture
    # ------------------------------------------------------------------

    def reevaluer_devises(self, date_arrete, *, pendant_cloture: bool = False,
                          session: Session | None = None) -> dict:
        """Convertit au cours du ``date_arrete`` les créances, dettes et
        trésoreries en devise. Idempotent pour une date donnée.

        Renvoie ``{"ecarts": écriture|None, "tresorerie": écriture|None,
        "provision": écriture|None}``."""
        jour = _date(date_arrete)

        def _t(s: Session) -> dict:
            ex = self._exercice_pour(s, jour)
            cle = jour.isoformat()
            resultat = {}
            for type_source in ("reevaluation", "reevaluation_tresorerie",
                                "provision_change"):
                deja = self._ecriture_source(s, type_source, cle)
                if deja is not None:
                    resultat[type_source] = resume_ecriture(deja, deja.lignes)
            if resultat:
                return {"ecarts": resultat.get("reevaluation"),
                        "tresorerie": resultat.get("reevaluation_tresorerie"),
                        "provision": resultat.get("provision_change"),
                        "cours_utilises": None}
            cours_utilises: dict = {}

            lettrables = set(s.scalars(select(Compte.numero).where(
                Compte.tenant_id == self.tenant_id, Compte.lettrable.is_(True))))
            comptes_devise = {c.numero: c.devise for c in s.scalars(select(Compte).where(
                Compte.tenant_id == self.tenant_id, Compte.devise.is_not(None)))}
            lettrees = select(LigneLettrage.ligne_id).where(
                LigneLettrage.tenant_id == self.tenant_id,
                LigneLettrage.deleted_at.is_(None))
            req = (select(LigneEcriture)
                   .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                   .where(LigneEcriture.tenant_id == self.tenant_id,
                          LigneEcriture.deleted_at.is_(None),
                          LigneEcriture.devise.is_not(None),
                          Ecriture.statut == Ecriture.VALIDEE,
                          Ecriture.exercice_id == ex.id,
                          Ecriture.date_ecriture <= jour,
                          LigneEcriture.id.not_in(lettrees)))
            tiers: dict[tuple, list[Decimal]] = {}
            tresor: dict[tuple, list[Decimal]] = {}
            noms: dict[tuple, str | None] = {}
            for lg in s.scalars(req):
                m = lg.montant_devise or ZERO
                signe_d = m if lg.debit else -m
                if lg.compte_numero in lettrables:
                    cle_g = (lg.compte_numero, lg.tiers_ref, lg.devise)
                    cible = tiers
                    noms.setdefault(cle_g, lg.tiers_nom)
                elif comptes_devise.get(lg.compte_numero) == lg.devise:
                    cle_g = (lg.compte_numero, lg.devise)
                    cible = tresor
                else:
                    continue
                acc = cible.setdefault(cle_g, [ZERO, ZERO])
                acc[0] += lg.debit - lg.credit
                acc[1] += signe_d

            lignes_ecarts: list[Ligne] = []
            perte_latente = ZERO
            for (compte, ref, devise), (x, d) in sorted(
                    tiers.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2])):
                if d == ZERO and x == ZERO:
                    continue
                ecart = convertir(d, self._cours_cloture(
                    s, devise, jour, cours_utilises, pendant_cloture)) - x
                if ecart == ZERO:
                    continue
                lib = f"Écart de conversion {devise} au {jour:%d/%m/%Y}"
                commun = {"tiers_ref": ref, "tiers_nom": noms.get((compte, ref, devise)),
                          "devise": devise, "montant_devise": ZERO, "libelle": lib}
                if ecart > 0:
                    lignes_ecarts += [
                        Ligne(compte=compte, debit=ecart, **commun),
                        Ligne(compte=self._parametre(s, "compte_ecart_conversion_passif"),
                              credit=ecart, libelle=lib)]
                else:
                    perte_latente += -ecart
                    lignes_ecarts += [
                        Ligne(compte=self._parametre(s, "compte_ecart_conversion_actif"),
                              debit=-ecart, libelle=lib),
                        Ligne(compte=compte, credit=-ecart, **commun)]
            journal_od = self._parametre(s, "journal_od")
            if lignes_ecarts:
                resultat["ecarts"] = self._saisir(
                    journal_od, jour, f"Écarts de conversion au {jour:%d/%m/%Y}",
                    lignes_ecarts, reference=None, valider=True,
                    source_type="reevaluation", source_id=cle,
                    type_ecriture=Ecriture.NORMALE,
                    ignorer_verrou=pendant_cloture, session=s)

            lignes_tresor: list[Ligne] = []
            for (compte, devise), (x, d) in sorted(tresor.items()):
                ecart = convertir(d, self._cours_cloture(
                    s, devise, jour, cours_utilises, pendant_cloture)) - x
                if ecart == ZERO:
                    continue
                lib = f"Conversion {devise} au cours de clôture"
                if ecart > 0:
                    lignes_tresor += [
                        Ligne(compte=compte, debit=ecart, devise=devise,
                              montant_devise=ZERO, libelle=lib),
                        Ligne(compte=self._parametre(s, "compte_gain_change_financiere"),
                              credit=ecart, libelle=lib)]
                else:
                    lignes_tresor += [
                        Ligne(compte=self._parametre(s, "compte_perte_change_financiere"),
                              debit=-ecart, libelle=lib),
                        Ligne(compte=compte, credit=-ecart, devise=devise,
                              montant_devise=ZERO, libelle=lib)]
            if lignes_tresor:
                resultat["tresorerie"] = self._saisir(
                    journal_od, jour, f"Conversion des trésoreries en devise au "
                    f"{jour:%d/%m/%Y}", lignes_tresor, reference=None, valider=True,
                    source_type="reevaluation_tresorerie", source_id=cle,
                    type_ecriture=Ecriture.NORMALE,
                    ignorer_verrou=pendant_cloture, session=s)

            # Provision pour perte de change = pertes latentes de la date.
            compte_prov = self._parametre(s, "compte_provision_change")
            soldes = self._soldes_a_date(s, ex, jour)
            actuelle = -soldes.get(compte_prov, ZERO)          # solde créditeur
            variation = perte_latente - actuelle
            if variation:
                lib = f"Provision pour perte de change au {jour:%d/%m/%Y}"
                if variation > 0:
                    lignes_p = [Ligne(compte=self._parametre(
                        s, "compte_dotation_provision_financiere"), debit=variation),
                        Ligne(compte=compte_prov, credit=variation)]
                else:
                    lignes_p = [Ligne(compte=compte_prov, debit=-variation),
                                Ligne(compte=self._parametre(
                                    s, "compte_reprise_provision_financiere"),
                                      credit=-variation)]
                resultat["provision"] = self._saisir(
                    journal_od, jour, lib, lignes_p, reference=None, valider=True,
                    source_type="provision_change", source_id=cle,
                    type_ecriture=Ecriture.NORMALE,
                    ignorer_verrou=pendant_cloture, session=s)
            return {"ecarts": resultat.get("ecarts"),
                    "tresorerie": resultat.get("tresorerie"),
                    "provision": resultat.get("provision"),
                    "cours_utilises": cours_utilises}

        return self._executer_avec_reprise_numerotation(_t, session)

    def _a_reevaluer(self, s: Session, ex, jour: date) -> bool:
        """Reste-t-il des lignes en devise ouvertes à la date ?"""
        lettrees = select(LigneLettrage.ligne_id).where(
            LigneLettrage.tenant_id == self.tenant_id,
            LigneLettrage.deleted_at.is_(None))
        return s.scalar(select(LigneEcriture.id)
                        .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                        .where(LigneEcriture.tenant_id == self.tenant_id,
                               LigneEcriture.deleted_at.is_(None),
                               LigneEcriture.devise.is_not(None),
                               Ecriture.statut == Ecriture.VALIDEE,
                               Ecriture.exercice_id == ex.id,
                               Ecriture.date_ecriture <= jour,
                               LigneEcriture.id.not_in(lettrees)).limit(1)) is not None
