"""
Système minimal de trésorerie (SMT) — très petites entités.

AUDCIF révisé, art. 13 : sauf option pour le système normal, relèvent du SMT
les entités dont le chiffre d'affaires annuel hors taxes est inférieur à :

- 60 millions F CFA pour les activités de négoce ;
- 40 millions F CFA pour les activités artisanales et assimilées ;
- 30 millions F CFA pour les activités de services.

Le SMT repose sur une comptabilité de trésorerie (recettes / dépenses) et des
états simplifiés (bilan et compte de résultat SMT, notes annexes). Ici, ces
états sont DÉRIVÉS du même grand livre que le système normal : une boutique
qui grandit et passe au système normal ne ressaisit rien.

À faire valider par l'expert-comptable : les intitulés des postes SMT
ci-dessous suivent la logique du modèle SMT (actif / passif simplifiés,
recettes / dépenses par nature) ; les codes officiels de la liasse SMT du pays
peuvent différer.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .erreurs import ParametreInvalide
from .etats import CLASSES_BILAN, CLASSES_GESTION
from .etats_financiers import _plus_long_prefixe
from .modeles import Compte, Ecriture, LigneEcriture
from .noyau import _date
from .socle import ZERO

SEUILS_SMT: dict[str, Decimal] = {
    "negoce": Decimal("60000000"),
    "artisanat": Decimal("40000000"),
    "services": Decimal("30000000"),
}


def eligible_smt(chiffre_affaires_ht, activite: str) -> bool:
    """L'entité relève-t-elle du SMT (seuils de l'art. 13 AUDCIF) ?"""
    activite = str(activite or "").strip().lower()
    if activite not in SEUILS_SMT:
        raise ParametreInvalide(
            f"Activité inconnue : {activite!r} (attendu : {', '.join(SEUILS_SMT)}).")
    return Decimal(str(chiffre_affaires_ht)) < SEUILS_SMT[activite]


# Bilan SMT : préfixe -> (poste si débiteur, poste si créditeur)
LIBELLES_BILAN_SMT = {
    "A1": "Immobilisations (valeur nette)",
    "A2": "Stocks",
    "A3": "Clients",
    "A4": "Autres créances",
    "A5": "Trésorerie (caisse, banque, monnaie électronique)",
    "AT": "TOTAL ACTIF",
    "P1": "Compte de l'exploitant / capital",
    "P2": "Résultat de l'exercice",
    "P3": "Emprunts",
    "P4": "Fournisseurs",
    "P5": "Autres dettes",
    "PT": "TOTAL PASSIF",
}
REGLES_BILAN_SMT: dict[str, tuple[str | None, str | None]] = {
    "1": (None, "P1"), "13": (None, "P2"), "16": (None, "P3"), "17": (None, "P3"),
    "18": (None, "P3"), "19": (None, "P5"),
    "2": ("A1", None),
    "3": ("A2", None),
    "41": ("A3", "P5"), "40": ("A4", "P4"), "4": ("A4", "P5"),
    "5": ("A5", "P5"), "59": ("A5", None),
}

LIBELLES_RESULTAT_SMT = {
    "R1": "Ventes de marchandises",
    "R2": "Ventes de produits et de services",
    "R3": "Autres recettes",
    "RT": "TOTAL RECETTES",
    "D1": "Achats de marchandises et de matières",
    "D2": "Variation des stocks",
    "D3": "Transports et services extérieurs (loyer, électricité, téléphone…)",
    "D4": "Impôts et taxes",
    "D5": "Charges de personnel",
    "D6": "Rémunération de l'exploitant",
    "D7": "Frais financiers",
    "D8": "Dotations aux amortissements et provisions",
    "D9": "Autres dépenses",
    "DT": "TOTAL DÉPENSES",
    "RN": "RÉSULTAT DE L'EXERCICE",
}
REGLES_RESULTAT_SMT: dict[str, str] = {
    "701": "R1", "70": "R2", "7": "R3", "82": "R3", "84": "R3", "86": "R3",
    "88": "R3",
    "601": "D1", "602": "D1", "604": "D1", "605": "D3", "608": "D1",
    "603": "D2", "60": "D1",
    "61": "D3", "62": "D3", "63": "D3", "64": "D4", "666": "D6", "66": "D5",
    "67": "D7", "68": "D8", "69": "D8", "65": "D9", "6": "D9",
    "8": "D9", "89": "D4",
}


class SmtMixin:
    """États du système minimal de trésorerie de ``Comptabilite``."""

    def etats_smt(self, exercice, *, activite: str | None = None,
                  session: Session | None = None) -> dict:
        """Bilan et compte de résultat SMT de l'exercice.

        Avec ``activite`` (negoce, artisanat, services), indique en plus si le
        chiffre d'affaires de l'exercice permet de rester au SMT."""

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code_ou_date(s, exercice)
            soldes = self._soldes_a_date(s, ex, ex.date_fin)
            postes: dict[str, Decimal] = {}
            resultat = ZERO
            for numero, solde in soldes.items():
                if not solde:
                    continue
                if numero.startswith(CLASSES_GESTION):
                    resultat -= solde
                    continue
                if not numero.startswith(CLASSES_BILAN):
                    continue
                actif, passif = _plus_long_prefixe(numero, REGLES_BILAN_SMT)
                if numero.startswith(("28", "29", "39", "49", "59")):
                    # correctifs d'actif : en déduction du poste brut
                    poste = {"2": "A1", "3": "A2", "4": "A4", "5": "A5"}[numero[0]]
                    if numero.startswith("491"):
                        poste = "A3"
                    postes[poste] = postes.get(poste, ZERO) + solde
                    continue
                if actif is None:
                    postes[passif] = postes.get(passif, ZERO) - solde
                elif passif is None or solde >= 0:
                    postes[actif] = postes.get(actif, ZERO) + solde
                else:
                    postes[passif] = postes.get(passif, ZERO) - solde
            postes["P2"] = postes.get("P2", ZERO) + resultat
            actif_total = sum((postes.get(c, ZERO) for c in ("A1", "A2", "A3", "A4", "A5")),
                              ZERO)
            passif_total = sum((postes.get(c, ZERO) for c in ("P1", "P2", "P3", "P4", "P5")),
                               ZERO)
            bilan = [{"code": c, "libelle": LIBELLES_BILAN_SMT[c],
                      "montant": (actif_total if c == "AT" else passif_total
                                  if c == "PT" else postes.get(c, ZERO))}
                     for c in LIBELLES_BILAN_SMT]

            mouvements = self._agreger(
                s, ex, exclure_types=(Ecriture.A_NOUVEAU, Ecriture.CLOTURE))
            cr: dict[str, Decimal] = {}
            for numero, (d, c) in mouvements.items():
                if not numero.startswith(CLASSES_GESTION):
                    continue
                # Classe 8 : produits (82, 84, 86, 88) en recettes, le reste
                # (valeurs de cession, charges HAO, impôt) en dépenses.
                code = _plus_long_prefixe(numero, REGLES_RESULTAT_SMT)
                valeur = (c - d) if code.startswith("R") else (d - c)
                cr[code] = cr.get(code, ZERO) + valeur
            recettes = sum((cr.get(c, ZERO) for c in ("R1", "R2", "R3")), ZERO)
            depenses = sum((cr.get(c, ZERO) for c in
                            ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9")), ZERO)
            cr["RT"], cr["DT"], cr["RN"] = recettes, depenses, recettes - depenses
            compte_resultat = [{"code": c, "libelle": LIBELLES_RESULTAT_SMT[c],
                                "montant": cr.get(c, ZERO)} for c in LIBELLES_RESULTAT_SMT]
            ca = cr.get("R1", ZERO) + cr.get("R2", ZERO)
            return {
                "exercice": ex.code,
                "bilan": bilan, "compte_de_resultat": compte_resultat,
                "total_actif": actif_total, "total_passif": passif_total,
                "equilibre": actif_total == passif_total,
                "resultat": cr["RN"], "chiffre_affaires": ca,
                "eligible_smt": (eligible_smt(ca, activite) if activite else None),
            }
        return self._lire(_t, session)

    def livre_recettes_depenses(self, debut, fin, *,
                                comptes_tresorerie: list[str] | None = None,
                                session: Session | None = None) -> dict:
        """Livre de trésorerie du SMT : chaque encaissement (recette) et
        décaissement (dépense), sa nature et le solde progressif.

        Les virements internes (d'un compte de trésorerie à un autre) sont
        exclus : ce ne sont ni des recettes ni des dépenses."""
        debut, fin = _date(debut, champ="début"), _date(fin, champ="fin")

        def _t(s: Session) -> dict:
            ex = self._exercice_couvrant(s, debut, fin)
            ouverture = dict(self._soldes_ouverture(s, ex))
            for n, (d, c) in self._agreger_strict_avant(s, ex, debut).items():
                ouverture[n] = ouverture.get(n, ZERO) + d - c

            def est_tresorerie(numero: str) -> bool:
                if comptes_tresorerie:
                    return numero in comptes_tresorerie
                return numero.startswith(("51", "52", "53", "54", "55", "57"))

            libelles = dict(s.execute(select(Compte.numero, Compte.libelle).where(
                Compte.tenant_id == self.tenant_id)).all())
            ecritures = s.scalars(
                select(Ecriture).where(
                    Ecriture.tenant_id == self.tenant_id,
                    Ecriture.statut == Ecriture.VALIDEE,
                    Ecriture.exercice_id == ex.id,
                    Ecriture.date_ecriture >= debut,
                    Ecriture.date_ecriture <= fin,
                    Ecriture.type_ecriture.not_in((Ecriture.A_NOUVEAU,)))
                .order_by(Ecriture.date_ecriture, Ecriture.journal_code,
                          Ecriture.poste, Ecriture.sequence))
            solde = sum((v for n, v in ouverture.items() if est_tresorerie(n)), ZERO)
            solde_initial = solde
            mouvements = []
            total_recettes = total_depenses = ZERO
            par_nature: dict[str, list[Decimal]] = {}
            for e in ecritures:
                lignes = list(e.lignes)
                tresor = [lg for lg in lignes if est_tresorerie(lg.compte_numero)]
                autres = [lg for lg in lignes if not est_tresorerie(lg.compte_numero)
                          and not lg.compte_numero.startswith("58")]
                if not tresor or not autres:
                    continue            # pas de trésorerie, ou virement interne
                net = sum((lg.debit - lg.credit for lg in tresor), ZERO)
                if not net:
                    continue
                principale = max(autres, key=lambda lg: lg.debit + lg.credit)
                nature = libelles.get(principale.compte_numero[:3]) or \
                    libelles.get(principale.compte_numero, "")
                recette = net if net > 0 else ZERO
                depense = -net if net < 0 else ZERO
                solde += net
                total_recettes += recette
                total_depenses += depense
                acc = par_nature.setdefault(nature, [ZERO, ZERO])
                acc[0] += recette
                acc[1] += depense
                mouvements.append({
                    "date": e.date_ecriture, "numero": e.numero,
                    "libelle": e.libelle, "reference": e.reference,
                    "compte_tresorerie": tresor[0].compte_numero,
                    "contrepartie": principale.compte_numero, "nature": nature,
                    "recette": recette, "depense": depense, "solde": solde})
            return {"debut": debut, "fin": fin, "solde_initial": solde_initial,
                    "mouvements": mouvements, "total_recettes": total_recettes,
                    "total_depenses": total_depenses, "solde_final": solde,
                    "par_nature": [{"nature": k, "recettes": v[0], "depenses": v[1]}
                                   for k, v in sorted(par_nature.items())]}
        return self._lire(_t, session)
