"""
Comptabilité analytique par sections.

Une section (rayon, activité, magasin, classe d'une école, service d'une
clinique…) se porte sur les lignes de charges et de produits. Le résultat
analytique ventile le résultat de la période par section ; la part sans
section reste visible (« Non affecté »), si bien que la somme des sections
redonne toujours le résultat comptable.

    compta.creer_section("QUINC", "Quincaillerie", axe="RAYON")
    compta.comptabiliser_vente(Vente(..., lignes=[LigneMontant("11800", section="QUINC")]))
    compta.resultat_analytique("2026-01-01", "2026-12-31", axe="RAYON")
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .erreurs import EcritureInvalide
from .modeles import Ecriture, LigneEcriture, Section, id_deterministe
from .noyau import _date, _texte
from .socle import INCLURE_SUPPRIMES, ZERO

NON_AFFECTE = "Non affecté"


class AnalytiqueMixin:
    """Sections et résultat analytique de ``Comptabilite``."""

    def creer_section(self, code: str, libelle: str, *, axe: str = "GENERAL",
                      session: Session | None = None) -> dict:
        code = _texte(code, champ="code section", max_len=30).upper()
        if not all(c.isalnum() or c in "-_" for c in code):
            raise EcritureInvalide("Code section : lettres, chiffres, - et _.")
        libelle = _texte(libelle, champ="libellé", max_len=120)
        axe = _texte(axe, champ="axe", max_len=30).upper()

        def _t(s: Session) -> dict:
            sid = id_deterministe(self.tenant_id, "section", code)
            sec = s.get(Section, sid, execution_options=INCLURE_SUPPRIMES)
            if sec is not None and sec.deleted_at is None and sec.actif:
                raise EcritureInvalide(f"La section {code} existe déjà.")
            if sec is None:
                sec = Section(id=sid, tenant_id=self.tenant_id, code=code,
                              libelle=libelle, axe=axe)
                s.add(sec)
            sec.libelle, sec.axe, sec.actif = libelle, axe, True
            sec.deleted_at = None
            sec.is_synced = False
            return self._section_dict(sec)
        return self._executer(_t, session)

    def desactiver_section(self, code: str, *, session: Session | None = None) -> None:
        def _t(s: Session) -> None:
            sec = s.get(Section, id_deterministe(self.tenant_id, "section",
                                                 str(code).strip().upper()))
            if sec is None:
                raise EcritureInvalide(f"Section {code!r} introuvable.")
            sec.actif = False
            sec.is_synced = False
        self._executer(_t, session)

    @staticmethod
    def _section_dict(sec: Section) -> dict:
        return {"code": sec.code, "libelle": sec.libelle, "axe": sec.axe,
                "actif": sec.actif}

    def sections(self, *, axe: str | None = None,
                 session: Session | None = None) -> list[dict]:
        def _t(s: Session) -> list[dict]:
            req = select(Section).where(Section.tenant_id == self.tenant_id)
            if axe:
                req = req.where(Section.axe == axe.upper())
            return [self._section_dict(x) for x in s.scalars(
                req.order_by(Section.axe, Section.code))]
        return self._lire(_t, session)

    def resultat_analytique(self, debut, fin, *, axe: str | None = None,
                            detail: bool = False,
                            session: Session | None = None) -> dict:
        """Produits, charges et résultat par section sur la période.

        Avec ``axe``, seules les sections de cet axe sont ventilées ; le reste
        tombe en « Non affecté ». ``detail=True`` ajoute le détail par compte."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> dict:
            ex = self._exercice_couvrant(s, debut, fin)
            sections = {x.code: x for x in s.scalars(
                select(Section).where(Section.tenant_id == self.tenant_id))}
            retenues = {c for c, x in sections.items()
                        if axe is None or x.axe == axe.upper()}
            req = (select(LigneEcriture.section, LigneEcriture.compte_numero,
                          LigneEcriture.debit, LigneEcriture.credit)
                   .join(Ecriture, Ecriture.id == LigneEcriture.ecriture_id)
                   .where(LigneEcriture.tenant_id == self.tenant_id,
                          LigneEcriture.deleted_at.is_(None),
                          Ecriture.statut == Ecriture.VALIDEE,
                          Ecriture.exercice_id == ex.id,
                          Ecriture.date_ecriture >= debut,
                          Ecriture.date_ecriture <= fin,
                          Ecriture.type_ecriture.not_in(
                              (Ecriture.A_NOUVEAU, Ecriture.CLOTURE)),
                          LigneEcriture.compte_numero.startswith("6")
                          | LigneEcriture.compte_numero.startswith("7")
                          | LigneEcriture.compte_numero.startswith("8")))
            par_section: dict[str, dict] = {}
            for section, numero, d, c in s.execute(req):
                cle = section if section in retenues else NON_AFFECTE
                ligne = par_section.setdefault(cle, {
                    "section": cle,
                    "libelle": sections[cle].libelle if cle in sections else NON_AFFECTE,
                    "axe": sections[cle].axe if cle in sections else None,
                    "produits": ZERO, "charges": ZERO, "comptes": {}})
                net = (c or ZERO) - (d or ZERO)          # + = produit
                if numero.startswith("7") or (numero.startswith("8")
                                              and numero[:2] in ("82", "84", "86", "88")):
                    ligne["produits"] += net
                else:
                    ligne["charges"] -= net
                if detail:
                    ligne["comptes"][numero] = ligne["comptes"].get(numero, ZERO) + net
            lignes = []
            total = {"produits": ZERO, "charges": ZERO, "resultat": ZERO}
            for cle in sorted(par_section, key=lambda k: (k == NON_AFFECTE, k)):
                l = par_section[cle]
                l["resultat"] = l["produits"] - l["charges"]
                if l["produits"]:
                    l["marge_pct"] = (l["resultat"] * 100 / l["produits"]).quantize(
                        Decimal("0.1"))
                else:
                    l["marge_pct"] = None
                if not detail:
                    del l["comptes"]
                else:
                    l["comptes"] = [{"compte": k, "montant": v}
                                    for k, v in sorted(l["comptes"].items())]
                lignes.append(l)
                for k in total:
                    total[k] += l[k]
            return {"debut": debut, "fin": fin, "axe": axe, "sections": lignes,
                    "total": total}
        return self._lire(_t, session)
