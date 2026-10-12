"""
Chaîne d'empreintes (inaltérabilité vérifiable).

Chaque écriture validée porte l'empreinte SHA-256 de son contenu canonique ET
de l'empreinte de l'écriture précédente de la même séquence
(tenant, journal, exercice, poste). Modifier une seule ligne en base — même
directement en SQL, hors de l'application — casse la chaîne à cet endroit et
``verifier_integrite`` le désigne.

La chaîne est PAR POSTE : un poste hors-ligne ne connaît que ses propres
écritures, il ne peut donc chaîner que celles-là. Chaque chaîne reste
vérifiable partout après synchronisation.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

GENESE = "0" * 64


def _centimes(valeur) -> str:
    if valeur is None:
        return ""
    return str(int((Decimal(str(valeur)) * 100).to_integral_value()))


def contenu_canonique(ecriture, lignes, exercice_code: str,
                      empreinte_precedente: str) -> str:
    """Représentation déterministe (indépendante de la base et du poste)."""
    donnees = {
        "tenant": ecriture.tenant_id,
        "journal": ecriture.journal_code,
        "exercice": exercice_code,
        "poste": ecriture.poste,
        "sequence": ecriture.sequence,
        "numero": ecriture.numero,
        "date": ecriture.date_ecriture.isoformat(),
        "libelle": ecriture.libelle,
        "reference": ecriture.reference or "",
        "type": ecriture.type_ecriture,
        "source": [ecriture.source_type or "", ecriture.source_id or ""],
        "contrepasse": ecriture.contrepasse_id or "",
        "document_lie": ecriture.document_lie or "",
        "lignes": [
            [lg.ordre, lg.compte_numero, _centimes(lg.debit),
             _centimes(lg.credit), lg.tiers_ref or "", lg.libelle,
             lg.taxe_code or "", _centimes(lg.base_taxe),
             lg.date_echeance.isoformat() if lg.date_echeance else ""]
            for lg in sorted(lignes, key=lambda x: (x.ordre, x.id))
        ],
        "precedente": empreinte_precedente,
    }
    return json.dumps(donnees, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def calculer_empreinte(ecriture, lignes, exercice_code: str,
                       empreinte_precedente: str) -> str:
    contenu = contenu_canonique(ecriture, lignes, exercice_code,
                                empreinte_precedente)
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()
