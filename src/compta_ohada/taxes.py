"""
Ventilation HT / TVA d'un document (vente ou achat).

Règle d'arrondi alignée sur les ERP Beyrey (facture_pdf) : la TVA est
calculée SUR LE TOTAL de chaque taux, arrondie au centime (demi supérieur),
puis répartie entre les lignes au prorata (méthode du plus fort reste). Le
total de la facture imprimée et celui de l'écriture sont donc identiques au
centime près, quelle que soit la façon dont les lignes sont saisies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .erreurs import EcritureInvalide
from .socle import CENTIME, ZERO, montant


@dataclass(frozen=True)
class LigneVentilee:
    compte: str
    taxe_code: str | None
    ht: Decimal
    ttc: Decimal


@dataclass(frozen=True)
class TaxeVentilee:
    code: str
    compte: str
    taux: Decimal
    base: Decimal
    tva: Decimal


def _repartir(total: Decimal, poids: list[Decimal]) -> list[Decimal]:
    """Répartit ``total`` au prorata de ``poids`` (plus fort reste, au centime)."""
    somme = sum(poids, ZERO)
    if not poids:
        return []
    if somme == ZERO:
        if total != ZERO:
            raise EcritureInvalide("Répartition impossible : poids nuls.")
        return [ZERO for _ in poids]
    bruts = [total * p / somme for p in poids]
    arrondis = [b.quantize(CENTIME, rounding="ROUND_DOWN") if b >= 0
                else -((-b).quantize(CENTIME, rounding="ROUND_DOWN")) for b in bruts]
    reste = total - sum(arrondis, ZERO)
    pas = CENTIME if reste > 0 else -CENTIME
    ordre = sorted(range(len(poids)),
                   key=lambda i: (abs(bruts[i] - arrondis[i]), abs(poids[i])),
                   reverse=True)
    i = 0
    while reste != ZERO:
        arrondis[ordre[i % len(ordre)]] += pas
        reste -= pas
        i += 1
    return arrondis


def ventiler(lignes: list[tuple[str, str | None, Decimal]],
             taxes: dict[str, tuple[Decimal, str]], *, prix_ttc: bool
             ) -> tuple[list[LigneVentilee], list[TaxeVentilee]]:
    """Ventile des lignes ``(compte, code_taxe|None, montant)``.

    ``taxes`` : code -> (taux en %, compte de TVA). Un code absent de
    ``taxes`` ou ``None`` signifie « hors champ » (pas de TVA).
    Renvoie les lignes avec HT/TTC et une ligne par taxe (base et TVA)."""
    groupes: dict[str | None, list[int]] = {}
    for i, (_, code, _) in enumerate(lignes):
        groupes.setdefault(code, []).append(i)
    ht = [ZERO] * len(lignes)
    ttc = [ZERO] * len(lignes)
    resultat_taxes: list[TaxeVentilee] = []
    for code, indices in groupes.items():
        montants = [montant(lignes[i][2]) for i in indices]
        total = sum(montants, ZERO)
        if total < ZERO:
            raise EcritureInvalide(
                "Montant net négatif pour un taux de TVA : les remises "
                "dépassent les ventes. Saisissez un avoir à la place.")
        if code is None or code not in taxes:
            for i, m in zip(indices, montants):
                ht[i] = ttc[i] = m
            continue
        taux, compte_tva = taxes[code]
        t = taux / Decimal(100)
        if prix_ttc:
            total_ht = (total / (1 + t)).quantize(CENTIME, rounding=ROUND_HALF_UP)
            total_tva = total - total_ht
        else:
            total_ht = total
            total_tva = (total * t).quantize(CENTIME, rounding=ROUND_HALF_UP)
        parts = _repartir(total_tva, montants)
        for i, m, part in zip(indices, montants, parts):
            if prix_ttc:
                ht[i], ttc[i] = m - part, m
            else:
                ht[i], ttc[i] = m, m + part
        resultat_taxes.append(TaxeVentilee(code=code, compte=compte_tva,
                                           taux=taux, base=total_ht,
                                           tva=total_tva))
    ventilees = [LigneVentilee(compte=c, taxe_code=code, ht=ht[i], ttc=ttc[i])
                 for i, (c, code, _) in enumerate(lignes)]
    return ventilees, resultat_taxes
