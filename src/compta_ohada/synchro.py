"""
Intégration à la synchronisation Offline-First des ERP.

L'hôte fusionne ``TABLES_SYNC`` dans son propre registre :

    from compta_ohada.synchro import TABLES_SYNC as TABLES_COMPTA
    TABLES_SYNC.update(TABLES_COMPTA)

et appelle ``valider_ligne_entrante`` dans son validateur métier pour refuser,
côté serveur, une écriture altérée en chemin (déséquilibrée, numéro ou
empreinte incohérents).

L'ordre du registre respecte les clés étrangères (parents avant enfants).
"""

from __future__ import annotations

from decimal import Decimal

from .erreurs import ErreurComptable
from .modeles import (Compte, Cours, Ecriture, Exercice, Immobilisation,
                      Journal, Lettrage, LigneEcriture, LigneLettrage, Parametre,
                      Section, Taxe)

TABLES_SYNC: dict[str, type] = {
    "compta_exercices": Exercice,
    "compta_comptes": Compte,
    "compta_journaux": Journal,
    "compta_taxes": Taxe,
    "compta_parametres": Parametre,
    "compta_cours": Cours,
    "compta_immobilisations": Immobilisation,
    "compta_sections": Section,
    "compta_ecritures": Ecriture,
    "compta_lignes": LigneEcriture,
    "compta_lettrages": Lettrage,
    "compta_lignes_lettrage": LigneLettrage,
}


def _montant(v) -> Decimal:
    return Decimal(str(v if v is not None else 0))


def valider_ligne_entrante(modele: type, valeurs: dict) -> None:
    """Garde-fous appliqués à une ligne reçue par la synchronisation.

    Ne remplace pas ``verifier_integrite`` (qui contrôle l'écriture ENTIÈRE,
    lignes comprises, une fois tout reçu) : ce sont les contrôles ligne à
    ligne, bon marché, qui arrêtent une donnée manifestement corrompue."""
    if modele is LigneEcriture:
        d, c = _montant(valeurs.get("debit")), _montant(valeurs.get("credit"))
        if d < 0 or c < 0 or (d and c) or (not d and not c):
            raise ErreurComptable("Ligne d'écriture reçue invalide (sens/montant).")
        numero = str(valeurs.get("compte_numero") or "")
        if not numero.isdigit():
            raise ErreurComptable("Ligne d'écriture reçue sans compte valide.")
    elif modele is Ecriture:
        if valeurs.get("statut") == Ecriture.VALIDEE:
            for champ in ("numero", "sequence", "empreinte", "empreinte_precedente"):
                if not valeurs.get(champ):
                    raise ErreurComptable(
                        f"Écriture validée reçue sans {champ} : refusée.")
            if len(str(valeurs["empreinte"])) != 64:
                raise ErreurComptable("Empreinte d'écriture reçue invalide.")
        if _montant(valeurs.get("total")) < 0:
            raise ErreurComptable("Total d'écriture négatif.")
