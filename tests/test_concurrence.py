"""Saisies simultanées sur le même poste (UI + worker de synchro, par exemple)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from compta_ohada import LigneMontant, Reglement, Vente
from conftest import D


def test_ventes_simultanees_sans_trou_ni_doublon(compta):
    def vendre(i):
        return compta.comptabiliser_vente(Vente(
            f"V{i}", date(2026, 5, 1), [LigneMontant("1180")],
            reglements=[Reglement("1180")]))["facture"]["numero"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        numeros = list(pool.map(vendre, range(30)))
    assert len(set(numeros)) == 30
    sequences = sorted(int(n.rsplit("-", 1)[1]) for n in numeros)
    assert sequences == list(range(1, 31))
    rapport = compta.verifier_integrite()
    assert rapport["ok"], rapport["anomalies"]
    assert compta.balance("2026-01-01", "2026-12-31")["totaux"]["debit"] == D(1180 * 60)
