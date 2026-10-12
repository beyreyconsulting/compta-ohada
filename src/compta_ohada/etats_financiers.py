"""
États financiers de synthèse SYSCOHADA révisé (système normal) :

- Bilan (actif brut / amortissements et dépréciations / net, passif) ;
- Compte de résultat (soldes intermédiaires XA à XI) ;
- Tableau des flux de trésorerie (TFT).

Le SYSCOHADA révisé (AUDCIF 2017, applicable depuis 2018) a REMPLACÉ le
TAFIRE de l'ancien système par le Tableau des flux de trésorerie : c'est donc
le TFT qui est produit ici.

Le classement compte -> poste se fait par le plus long préfixe. Les tables
ci-dessous sont l'endroit à faire relire par l'expert-comptable ; chaque état
renvoie aussi un contrôle d'équilibre (actif = passif ; trésorerie
d'ouverture + variation = trésorerie de clôture ; résultat du compte de
résultat = résultat du bilan).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from .etats import CLASSES_BILAN, CLASSES_GESTION
from .modeles import Ecriture, Exercice
from .noyau import _date
from .socle import ZERO


def _plus_long_prefixe(numero: str, table: dict[str, object]):
    for i in range(len(numero), 0, -1):
        cle = numero[:i]
        if cle in table:
            return table[cle]
    return None


# ---------------------------------------------------------------------------
# BILAN
# ---------------------------------------------------------------------------

LIBELLES_BILAN: dict[str, str] = {
    "AD": "Immobilisations incorporelles",
    "AE": "Frais de développement et de prospection",
    "AF": "Brevets, licences, logiciels et droits similaires",
    "AG": "Fonds commercial et droit au bail",
    "AH": "Autres immobilisations incorporelles",
    "AI": "Immobilisations corporelles",
    "AJ": "Terrains",
    "AK": "Bâtiments",
    "AL": "Aménagements, agencements et installations",
    "AM": "Matériel, mobilier et actifs biologiques",
    "AN": "Matériel de transport",
    "AP": "Avances et acomptes versés sur immobilisations",
    "AQ": "Immobilisations financières",
    "AR": "Titres de participation",
    "AS": "Autres immobilisations financières",
    "AZ": "TOTAL ACTIF IMMOBILISÉ",
    "BA": "Actif circulant HAO",
    "BB": "Stocks et encours",
    "BG": "Créances et emplois assimilés",
    "BH": "Fournisseurs, avances versées",
    "BI": "Clients",
    "BJ": "Autres créances",
    "BK": "TOTAL ACTIF CIRCULANT",
    "BQ": "Titres de placement",
    "BR": "Valeurs à encaisser",
    "BS": "Banques, chèques postaux, caisse et assimilés",
    "BT": "TOTAL TRÉSORERIE-ACTIF",
    "BU": "Écart de conversion-Actif",
    "BZ": "TOTAL GÉNÉRAL",
    "CA": "Capital",
    "CB": "Apporteurs capital non appelé (-)",
    "CD": "Primes liées au capital social",
    "CE": "Écarts de réévaluation",
    "CF": "Réserves indisponibles",
    "CG": "Réserves libres",
    "CH": "Report à nouveau (+ ou -)",
    "CJ": "Résultat net de l'exercice (bénéfice + ou perte -)",
    "CL": "Subventions d'investissement",
    "CM": "Provisions réglementées",
    "CP": "TOTAL CAPITAUX PROPRES ET RESSOURCES ASSIMILÉES",
    "DA": "Emprunts et dettes financières diverses",
    "DB": "Dettes de location-acquisition",
    "DC": "Provisions pour risques et charges",
    "DD": "TOTAL DETTES FINANCIÈRES ET RESSOURCES ASSIMILÉES",
    "DF": "TOTAL RESSOURCES STABLES",
    "DH": "Dettes circulantes HAO",
    "DI": "Clients, avances reçues",
    "DJ": "Fournisseurs d'exploitation",
    "DK": "Dettes fiscales et sociales",
    "DM": "Autres dettes",
    "DN": "Provisions pour risques à court terme",
    "DP": "TOTAL PASSIF CIRCULANT",
    "DQ": "Banques, crédits d'escompte",
    "DR": "Banques, établissements financiers et crédits de trésorerie",
    "DT": "TOTAL TRÉSORERIE-PASSIF",
    "DV": "Écart de conversion-Passif",
    "DZ": "TOTAL GÉNÉRAL",
}

STRUCTURE_ACTIF = (
    ("AD", ("AE", "AF", "AG", "AH")), ("AI", ("AJ", "AK", "AL", "AM", "AN")),
    ("AP", ()), ("AQ", ("AR", "AS")), ("AZ", ("AD", "AI", "AP", "AQ")),
    ("BA", ()), ("BB", ()), ("BG", ("BH", "BI", "BJ")), ("BK", ("BA", "BB", "BG")),
    ("BQ", ()), ("BR", ()), ("BS", ()), ("BT", ("BQ", "BR", "BS")), ("BU", ()),
    ("BZ", ("AZ", "BK", "BT", "BU")),
)
STRUCTURE_PASSIF = (
    ("CA", ()), ("CB", ()), ("CD", ()), ("CE", ()), ("CF", ()), ("CG", ()),
    ("CH", ()), ("CJ", ()), ("CL", ()), ("CM", ()),
    ("CP", ("CA", "CB", "CD", "CE", "CF", "CG", "CH", "CJ", "CL", "CM")),
    ("DA", ()), ("DB", ()), ("DC", ()), ("DD", ("DA", "DB", "DC")),
    ("DF", ("CP", "DD")),
    ("DH", ()), ("DI", ()), ("DJ", ()), ("DK", ()), ("DM", ()), ("DN", ()),
    ("DP", ("DH", "DI", "DJ", "DK", "DM", "DN")),
    ("DQ", ()), ("DR", ()), ("DT", ("DQ", "DR")), ("DV", ()),
    ("DZ", ("DF", "DP", "DT", "DV")),
)

def _ordre_affichage(structure) -> list[str]:
    """Ordre officiel : un sous-total « de regroupement » (AD, AI, BG…) est
    suivi de ses postes ; un total « de section » (AZ, BK, CP…) vient après."""
    sections = {"AZ", "BK", "BT", "BZ", "CP", "DD", "DF", "DP", "DT", "DZ"}
    ordre: list[str] = []
    for code, enfants in structure:
        if code in ordre:
            continue
        ordre.append(code)
        if enfants and code not in sections:
            ordre.extend(e for e in enfants if e not in ordre)
    return ordre


# Règles : préfixe -> (poste si solde débiteur [actif], poste si solde
# créditeur [passif]). ``None`` d'un côté = le poste de l'autre côté reçoit le
# solde avec son signe (ex. 109 : toujours au passif, en négatif).
REGLES_BILAN: dict[str, tuple[str | None, str | None]] = {
    # Capitaux propres (toujours au passif, signés)
    "10": (None, "CA"), "109": (None, "CB"), "105": (None, "CD"),
    "106": (None, "CE"), "111": (None, "CF"), "112": (None, "CF"),
    "113": (None, "CF"), "11": (None, "CG"), "12": (None, "CH"),
    "13": (None, "CJ"), "14": (None, "CL"), "15": (None, "CM"),
    "16": (None, "DA"), "18": (None, "DA"), "17": (None, "DB"),
    "19": (None, "DC"), "1": (None, "DA"),
    # Actif immobilisé
    "211": ("AE", None), "212": ("AF", None), "213": ("AF", None),
    "214": ("AF", None), "215": ("AG", None), "216": ("AG", None),
    "21": ("AH", None), "22": ("AJ", None), "234": ("AL", None),
    "235": ("AL", None), "238": ("AL", None), "23": ("AK", None),
    "245": ("AN", None), "24": ("AM", None), "25": ("AP", None),
    "26": ("AR", None), "27": ("AS", None), "2": ("AH", None),
    # Stocks
    "3": ("BB", None),
    # Tiers
    "409": ("BH", "DJ"), "40": ("BH", "DJ"),
    "419": ("BI", "DI"), "41": ("BI", "DI"),
    "42": ("BJ", "DK"), "43": ("BJ", "DK"), "44": ("BJ", "DK"),
    "45": ("BJ", "DM"), "46": ("BJ", "DM"), "47": ("BJ", "DM"),
    "476": ("BJ", "DM"), "477": ("BJ", "DM"),
    "478": ("BU", None), "479": (None, "DV"),
    "481": ("BA", "DH"), "482": ("BA", "DH"), "484": ("BA", "DH"),
    "485": ("BA", "DH"), "488": ("BA", "DH"), "48": ("BA", "DH"),
    "499": (None, "DN"), "4": ("BJ", "DM"),
    # Trésorerie
    "50": ("BQ", "DR"), "51": ("BR", "DR"), "52": ("BS", "DR"),
    "53": ("BS", "DR"), "54": ("BS", "DR"), "55": ("BS", "DR"),
    "57": ("BS", "DR"), "58": ("BS", "DR"),
    "565": ("BS", "DQ"), "56": ("BS", "DR"),
    "599": (None, "DN"), "5": ("BS", "DR"),
}

# Comptes correctifs d'actif (amortissements, dépréciations) : préfixe ->
# préfixe du compte brut dont ils réduisent la valeur.
def _brut_corrige(numero: str) -> str | None:
    if numero.startswith(("28", "29")) and len(numero) >= 3:
        return "2" + numero[2:]
    if numero.startswith("39"):
        return "3" + numero[2:] if len(numero) >= 3 else "3"
    if numero.startswith("49") and not numero.startswith("499"):
        cible = {"490": "409", "491": "411", "498": "485"}.get(numero[:3])
        return cible or "47"
    if numero.startswith("59") and not numero.startswith("599"):
        return {"590": "50", "591": "51"}.get(numero[:3], "52")
    return None


def classer_bilan(numero: str, solde: Decimal) -> tuple[str, str, Decimal]:
    """(poste, colonne, montant) pour un solde de compte de bilan.

    colonne : "brut" ou "amort" (actif), "passif"."""
    brut = _brut_corrige(numero)
    if brut is not None:
        poste_actif = _plus_long_prefixe(brut, REGLES_BILAN)[0] or "AH"
        return poste_actif, "amort", -solde
    actif, passif = _plus_long_prefixe(numero, REGLES_BILAN) or ("BJ", "DM")
    if actif is None:
        return passif, "passif", -solde
    if passif is None:
        return actif, "brut", solde
    if solde >= 0:
        return actif, "brut", solde
    return passif, "passif", -solde


# ---------------------------------------------------------------------------
# COMPTE DE RÉSULTAT
# ---------------------------------------------------------------------------

LIBELLES_RESULTAT: dict[str, str] = {
    "TA": "Ventes de marchandises", "RA": "Achats de marchandises",
    "RB": "Variation de stocks de marchandises", "XA": "MARGE COMMERCIALE",
    "TB": "Ventes de produits fabriqués", "TC": "Travaux, services vendus",
    "TD": "Produits accessoires", "XB": "CHIFFRE D'AFFAIRES",
    "TE": "Production stockée (ou déstockage)", "TF": "Production immobilisée",
    "TG": "Subventions d'exploitation", "TH": "Autres produits",
    "TI": "Transferts de charges d'exploitation",
    "RC": "Achats de matières premières et fournitures liées",
    "RD": "Variation de stocks de matières premières et fournitures liées",
    "RE": "Autres achats", "RF": "Variation de stocks d'autres approvisionnements",
    "RG": "Transports", "RH": "Services extérieurs", "RI": "Impôts et taxes",
    "RJ": "Autres charges", "XC": "VALEUR AJOUTÉE",
    "RK": "Charges de personnel", "XD": "EXCÉDENT BRUT D'EXPLOITATION",
    "TJ": "Reprises d'amortissements, provisions et dépréciations",
    "RL": "Dotations aux amortissements, aux provisions et dépréciations",
    "XE": "RÉSULTAT D'EXPLOITATION",
    "TK": "Revenus financiers et assimilés",
    "TL": "Reprises de provisions et dépréciations financières",
    "TM": "Transferts de charges financières",
    "RM": "Frais financiers et charges assimilées",
    "RN": "Dotations aux provisions et aux dépréciations financières",
    "XF": "RÉSULTAT FINANCIER", "XG": "RÉSULTAT DES ACTIVITÉS ORDINAIRES",
    "TN": "Produits des cessions d'immobilisations", "TO": "Autres produits HAO",
    "RO": "Valeurs comptables des cessions d'immobilisations",
    "RP": "Autres charges HAO", "XH": "RÉSULTAT HORS ACTIVITÉS ORDINAIRES",
    "RQ": "Participation des travailleurs", "RS": "Impôts sur le résultat",
    "XI": "RÉSULTAT NET",
}

REGLES_RESULTAT: dict[str, str] = {
    "601": "RA", "6031": "RB", "602": "RC", "6032": "RD", "604": "RE",
    "605": "RE", "608": "RE", "6033": "RF", "603": "RF", "60": "RE",
    "61": "RG", "62": "RH", "63": "RH", "64": "RI", "659": "RL", "65": "RJ",
    "66": "RK", "68": "RL", "69": "RL", "697": "RN", "679": "RN", "67": "RM",
    "6": "RJ",
    "701": "TA", "702": "TB", "703": "TB", "704": "TB", "705": "TC",
    "706": "TC", "707": "TD", "70": "TC", "71": "TG", "72": "TF", "73": "TE",
    "759": "TJ", "75": "TH", "779": "TL", "77": "TK", "787": "TM", "78": "TI",
    "797": "TL", "79": "TJ", "7": "TH",
    "81": "RO", "82": "TN", "83": "RP", "84": "TO", "85": "RP", "86": "TO",
    "87": "RQ", "88": "TO", "89": "RS", "8": "RP",
}

ORDRE_RESULTAT = (
    "TA", "RA", "RB", "XA", "TB", "TC", "TD", "XB", "TE", "TF", "TG", "TH", "TI",
    "RC", "RD", "RE", "RF", "RG", "RH", "RI", "RJ", "XC", "RK", "XD", "TJ", "RL",
    "XE", "TK", "TL", "TM", "RM", "RN", "XF", "XG", "TN", "TO", "RO", "RP", "XH",
    "RQ", "RS", "XI",
)


def _soldes_intermediaires(p: dict[str, Decimal]) -> dict[str, Decimal]:
    g = lambda *c: sum((p.get(x, ZERO) for x in c), ZERO)  # noqa: E731
    r = dict(p)
    r["XA"] = g("TA") - g("RA", "RB")
    r["XB"] = g("TA", "TB", "TC", "TD")
    r["XC"] = (r["XB"] + g("TE", "TF", "TG", "TH", "TI")
               - g("RA", "RB", "RC", "RD", "RE", "RF", "RG", "RH", "RI", "RJ"))
    r["XD"] = r["XC"] - g("RK")
    r["XE"] = r["XD"] + g("TJ") - g("RL")
    r["XF"] = g("TK", "TL", "TM") - g("RM", "RN")
    r["XG"] = r["XE"] + r["XF"]
    r["XH"] = g("TN", "TO") - g("RO", "RP")
    r["XI"] = r["XG"] + r["XH"] - g("RQ", "RS")
    return r


# ---------------------------------------------------------------------------
# TABLEAU DES FLUX DE TRÉSORERIE — partition exhaustive des comptes
# ---------------------------------------------------------------------------
# Chaque compte de classe 1 à 8 tombe dans EXACTEMENT un groupe. Comme le
# grand livre est équilibré, la variation de trésorerie est égale à l'opposé
# de la somme des variations de tous les autres groupes : le TFT « tombe
# juste » par construction, et l'écart de contrôle doit être nul.

GROUPES_TFT: dict[str, str] = {
    # Trésorerie
    "50": "T", "51": "T", "52": "T", "53": "T", "54": "T", "55": "T",
    "56": "T", "57": "T", "58": "T",
    # Éléments non monétaires de la CAFG (dotations/reprises et provisions)
    "69": "CAFG", "79": "CAFG", "85": "CAFG", "86": "CAFG", "659": "CAFG",
    "759": "CAFG", "679": "CAFG", "779": "CAFG", "839": "CAFG", "849": "CAFG",
    "19": "CAFG", "15": "CAFG", "29": "CAFG", "39": "CAFG", "49": "CAFG",
    "59": "CAFG",
    "6": "CAFG", "7": "CAFG", "8": "CAFG",
    # Investissement (y compris les mécanismes neutres : dotations aux
    # amortissements, VNC, reprises d'amortissements)
    "2": "INV", "681": "INV", "798": "INV", "81": "INV", "82": "INV",
    "654": "INV", "754": "INV", "481": "INV", "482": "INV", "485": "INV",
    "414": "INV",
    # Exploitation : besoin de financement
    "3": "STOCK",
    "409": "CREANCE", "41": "CREANCE", "419": "DETTE", "421": "CREANCE",
    "445": "CREANCE", "4449": "CREANCE", "476": "CREANCE", "478": "CREANCE",
    "40": "DETTE", "42": "DETTE", "43": "DETTE", "44": "DETTE", "45": "DETTE",
    "47": "DETTE", "4": "DETTE",
    "48": "HAO",
    # Financement
    "10": "CAPITAL", "109": "CAPITAL", "461": "CAPITAL", "467": "CAPITAL",
    "103": "PRELEVEMENT", "104": "PRELEVEMENT",
    "14": "SUBVENTION", "799": "SUBVENTION",
    "11": "DIVIDENDE", "12": "DIVIDENDE", "13": "DIVIDENDE", "465": "DIVIDENDE",
    "16": "EMPRUNT", "17": "EMPRUNT", "18": "EMPRUNT", "46": "EMPRUNT",
    "1": "EMPRUNT",
}

LIBELLES_TFT: dict[str, str] = {
    "ZA": "Trésorerie nette au 1er jour de l'exercice",
    "FA": "Capacité d'autofinancement globale (CAFG)",
    "FB": "Variation de l'actif circulant HAO",
    "FC": "Variation des stocks",
    "FD": "Variation des créances",
    "FE": "Variation du passif circulant",
    "ZB": "Flux de trésorerie provenant des activités opérationnelles",
    "FF": "Décaissements liés aux acquisitions d'immobilisations incorporelles",
    "FG": "Décaissements liés aux acquisitions d'immobilisations corporelles",
    "FH": "Décaissements liés aux acquisitions d'immobilisations financières",
    "FI": "Encaissements liés aux cessions d'immobilisations incorporelles et corporelles",
    "FJ": "Encaissements liés aux cessions d'immobilisations financières",
    "ZC": "Flux de trésorerie provenant des activités d'investissement",
    "FK": "Augmentations de capital par apports nouveaux",
    "FL": "Subventions d'investissement reçues",
    "FM": "Prélèvements sur le capital",
    "FN": "Dividendes versés",
    "ZD": "Flux de trésorerie provenant des capitaux propres",
    "FO": "Emprunts",
    "FP": "Autres dettes financières",
    "FQ": "Remboursements des emprunts et autres dettes financières",
    "ZE": "Flux de trésorerie provenant des capitaux étrangers",
    "ZF": "Flux de trésorerie provenant des activités de financement",
    "ZG": "VARIATION DE LA TRÉSORERIE NETTE DE LA PÉRIODE",
    "ZH": "Trésorerie nette de clôture",
}


def groupe_tft(numero: str) -> str | None:
    if numero.startswith("9"):
        return None
    return _plus_long_prefixe(numero, GROUPES_TFT)


class EtatsFinanciersMixin:
    """Bilan, compte de résultat et TFT de ``Comptabilite``."""

    def _exercice_par_code_ou_date(self, s: Session, exercice) -> Exercice:
        if isinstance(exercice, (date,)) :
            return self._exercice_pour(s, exercice)
        ex = self._exercice_par_code(s, str(exercice))
        if ex is None:
            from .erreurs import ExerciceIntrouvable
            raise ExerciceIntrouvable(f"Exercice {exercice!r} introuvable.")
        return ex

    # ------------------------------------------------------------------
    # Bilan
    # ------------------------------------------------------------------

    def _bilan_valeurs(self, s: Session, ex: Exercice, jour: date) -> dict:
        soldes = self._soldes_a_date(s, ex, jour)
        postes: dict[str, dict[str, Decimal]] = {}
        resultat_en_cours = ZERO
        non_classes = []
        for numero, solde in soldes.items():
            if not solde:
                continue
            if numero.startswith(CLASSES_GESTION):
                resultat_en_cours -= solde
                continue
            if not numero.startswith(CLASSES_BILAN):
                continue
            poste, colonne, m = classer_bilan(numero, solde)
            if poste is None:
                non_classes.append(numero)
                continue
            p = postes.setdefault(poste, {"brut": ZERO, "amort": ZERO, "passif": ZERO})
            p[colonne] += m
        if resultat_en_cours:
            p = postes.setdefault("CJ", {"brut": ZERO, "amort": ZERO, "passif": ZERO})
            p["passif"] += resultat_en_cours
        return {"postes": postes, "non_classes": non_classes,
                "resultat_en_cours": resultat_en_cours}

    @staticmethod
    def _assembler(structure, valeurs, *, actif: bool) -> dict[str, dict]:
        """Valeurs de chaque poste (feuilles et totaux), dans l'ordre d'affichage."""
        enfants_de = dict(structure)
        lignes: dict[str, dict] = {}

        def calculer(code: str) -> dict:
            if code in lignes:
                return lignes[code]
            enfants = enfants_de.get(code, ())
            if enfants:
                sous = [calculer(e) for e in enfants]
                if actif:
                    brut = sum((x["brut"] for x in sous), ZERO)
                    amort = sum((x["amort"] for x in sous), ZERO)
                    lignes[code] = {"brut": brut, "amort": amort, "net": brut - amort}
                else:
                    lignes[code] = {"net": sum((x["net"] for x in sous), ZERO)}
            else:
                v = valeurs.get(code, {"brut": ZERO, "amort": ZERO, "passif": ZERO})
                lignes[code] = ({"brut": v["brut"], "amort": v["amort"],
                                 "net": v["brut"] - v["amort"]} if actif
                                else {"net": v["passif"]})
            return lignes[code]

        for code, _ in structure:
            calculer(code)
        return lignes

    def bilan(self, exercice, *, date_arrete=None,
              session: Session | None = None) -> dict:
        """Bilan à la date d'arrêté (fin d'exercice par défaut), avec la
        colonne de l'exercice précédent s'il existe."""

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code_ou_date(s, exercice)
            jour = _date(date_arrete) if date_arrete else ex.date_fin
            if not ex.date_debut <= jour <= ex.date_fin:
                from .erreurs import ExerciceIntrouvable
                raise ExerciceIntrouvable("Date d'arrêté hors de l'exercice.")
            v = self._bilan_valeurs(s, ex, jour)
            actif = self._assembler(STRUCTURE_ACTIF, v["postes"], actif=True)
            passif = self._assembler(STRUCTURE_PASSIF, v["postes"], actif=False)
            precedent = self._exercice_precedent(s, ex)
            actif_n1 = passif_n1 = None
            if precedent is not None:
                v1 = self._bilan_valeurs(s, precedent, precedent.date_fin)
                actif_n1 = self._assembler(STRUCTURE_ACTIF, v1["postes"], actif=True)
                passif_n1 = self._assembler(STRUCTURE_PASSIF, v1["postes"], actif=False)
            def _lignes(struct, valeurs, n1, est_actif):
                sortie = []
                enfants_de = dict(struct)
                for code in _ordre_affichage(struct):
                    ligne = {"code": code, "libelle": LIBELLES_BILAN[code],
                             "total": bool(enfants_de.get(code)), **valeurs[code]}
                    ligne["net_n1"] = n1[code]["net"] if n1 else None
                    sortie.append(ligne)
                return sortie
            return {
                "exercice": ex.code, "date_arrete": jour,
                "actif": _lignes(STRUCTURE_ACTIF, actif, actif_n1, True),
                "passif": _lignes(STRUCTURE_PASSIF, passif, passif_n1, False),
                "total_actif": actif["BZ"]["net"],
                "total_passif": passif["DZ"]["net"],
                "equilibre": actif["BZ"]["net"] == passif["DZ"]["net"],
                "resultat": passif["CJ"]["net"],
                "comptes_non_classes": v["non_classes"],
            }
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Compte de résultat
    # ------------------------------------------------------------------

    def _resultat_valeurs(self, s: Session, ex: Exercice, debut: date,
                          fin: date) -> dict[str, Decimal]:
        mouvements = self._agreger(
            s, ex, debut=debut, fin=fin,
            exclure_types=(Ecriture.A_NOUVEAU, Ecriture.CLOTURE))
        postes: dict[str, Decimal] = {}
        for numero, (d, c) in mouvements.items():
            if not numero.startswith(CLASSES_GESTION):
                continue
            code = _plus_long_prefixe(numero, REGLES_RESULTAT)
            montant = (d - c) if code.startswith("R") else (c - d)
            postes[code] = postes.get(code, ZERO) + montant
        return _soldes_intermediaires(postes)

    def compte_de_resultat(self, exercice, *, debut=None, fin=None,
                           session: Session | None = None) -> dict:
        """Compte de résultat avec soldes intermédiaires (XA à XI) et
        colonne N-1."""

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code_ou_date(s, exercice)
            d = _date(debut) if debut else ex.date_debut
            f = _date(fin) if fin else ex.date_fin
            self._exercice_couvrant(s, d, f)
            valeurs = self._resultat_valeurs(s, ex, d, f)
            precedent = self._exercice_precedent(s, ex)
            n1 = (self._resultat_valeurs(s, precedent, precedent.date_debut,
                                         precedent.date_fin)
                  if precedent is not None else None)
            lignes = [{"code": c, "libelle": LIBELLES_RESULTAT[c],
                       "solde_intermediaire": c.startswith("X"),
                       "montant": valeurs.get(c, ZERO),
                       "montant_n1": (n1.get(c, ZERO) if n1 else None)}
                      for c in ORDRE_RESULTAT]
            return {"exercice": ex.code, "debut": d, "fin": f, "lignes": lignes,
                    "resultat_net": valeurs["XI"],
                    "chiffre_affaires": valeurs["XB"]}
        return self._lire(_t, session)

    # ------------------------------------------------------------------
    # Tableau des flux de trésorerie
    # ------------------------------------------------------------------

    def tableau_flux_tresorerie(self, exercice, *,
                                session: Session | None = None) -> dict:
        """TFT de l'exercice (méthode indirecte, à partir de la CAFG)."""

        def _t(s: Session) -> dict:
            ex = self._exercice_par_code_ou_date(s, exercice)
            ouverture = self._soldes_ouverture(s, ex)
            mouvements = self._agreger(
                s, ex, exclure_types=(Ecriture.A_NOUVEAU, Ecriture.CLOTURE))
            B: dict[str, Decimal] = {}          # variation nette (débit − crédit)
            debits: dict[str, Decimal] = {}
            credits: dict[str, Decimal] = {}
            for n, (d, c) in mouvements.items():
                B[n] = d - c
                debits[n] = d
                credits[n] = c
            somme = lambda groupe: sum(  # noqa: E731
                (v for n, v in B.items() if groupe_tft(n) == groupe), ZERO)
            def pref(dico, *prefixes, sauf=()):
                return sum((v for n, v in dico.items()
                            if n.startswith(prefixes) and not n.startswith(sauf)
                            and groupe_tft(n) == "INV"), ZERO)

            za = sum((v for n, v in ouverture.items() if groupe_tft(n) == "T"), ZERO)
            fa = -somme("CAFG")
            fb = -somme("HAO")
            fc = -somme("STOCK")
            fd = -somme("CREANCE")
            fe = -somme("DETTE")
            zb = fa + fb + fc + fd + fe

            zc = -somme("INV")
            ff = -pref(debits, "21")
            fg = (-pref(debits, "22", "23", "24", "25")
                  - pref(B, "481", "482"))
            fh = -pref(debits, "26", "27")
            fi = pref(credits, "82", "754", sauf=("826",)) - pref(B, "485", "414")
            fj = zc - (ff + fg + fh + fi)

            fk = -somme("CAPITAL")
            fl = -somme("SUBVENTION")
            fm = -somme("PRELEVEMENT")
            fn = -somme("DIVIDENDE")
            zd = fk + fl + fm + fn

            emprunts = [n for n in B if groupe_tft(n) == "EMPRUNT"]
            fo = sum((credits[n] for n in emprunts if n.startswith("16")), ZERO)
            fp = sum((credits[n] for n in emprunts if not n.startswith("16")), ZERO)
            fq = -sum((debits[n] for n in emprunts), ZERO)
            ze = fo + fp + fq
            zf = zd + ze
            zg = zb + zc + zf
            zh_calcul = za + zg
            zh_reel = za + somme("T")
            valeurs = {"ZA": za, "FA": fa, "FB": fb, "FC": fc, "FD": fd, "FE": fe,
                       "ZB": zb, "FF": ff, "FG": fg, "FH": fh, "FI": fi, "FJ": fj,
                       "ZC": zc, "FK": fk, "FL": fl, "FM": fm, "FN": fn, "ZD": zd,
                       "FO": fo, "FP": fp, "FQ": fq, "ZE": ze, "ZF": zf, "ZG": zg,
                       "ZH": zh_calcul}
            return {
                "exercice": ex.code,
                "lignes": [{"code": c, "libelle": LIBELLES_TFT[c],
                            "total": c.startswith("Z"), "montant": v}
                           for c, v in valeurs.items()],
                "tresorerie_ouverture": za,
                "tresorerie_cloture": zh_reel,
                "variation": zg,
                "ecart_controle": zh_reel - zh_calcul,
                "cafg": fa,
            }
        return self._lire(_t, session)
