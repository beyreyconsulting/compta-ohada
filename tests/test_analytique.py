"""Comptabilité analytique par sections."""

import pytest

from compta_ohada import (BulletinPaie, EcritureInvalide, Ligne, LigneMontant,
                          Vente)
from conftest import D, J


@pytest.fixture
def ca(compta):
    compta.creer_section("QUINC", "Quincaillerie", axe="RAYON")
    compta.creer_section("PEINT", "Peinture", axe="RAYON")
    compta.creer_section("BKO", "Magasin Bamako", axe="MAGASIN")
    return compta


def test_sections(ca):
    assert [s["code"] for s in ca.sections(axe="rayon")] == ["PEINT", "QUINC"]
    with pytest.raises(EcritureInvalide, match="existe"):
        ca.creer_section("QUINC", "Doublon")
    with pytest.raises(EcritureInvalide):
        ca.creer_section("A B", "Espace")
    ca.desactiver_section("PEINT")
    with pytest.raises(EcritureInvalide, match="désactivée"):
        ca.saisir("OD", J(2026, 1, 3), "x", [Ligne("6222", debit=1, section="PEINT"),
                                             Ligne("5711", credit=1)])


def test_section_reservee_aux_charges_et_produits(ca):
    with pytest.raises(EcritureInvalide, match="classes 6, 7, 8"):
        ca.saisir("OD", J(2026, 1, 3), "x", [Ligne("6222", debit=1),
                                             Ligne("5711", credit=1, section="QUINC")])
    with pytest.raises(EcritureInvalide, match="inconnue"):
        ca.saisir("OD", J(2026, 1, 3), "x", [Ligne("6222", debit=1, section="ZZ"),
                                             Ligne("5711", credit=1)])


def test_resultat_analytique(ca):
    ca.comptabiliser_vente(Vente("V1", J(2026, 2, 1), [
        LigneMontant("11800", section="QUINC"),
        LigneMontant("5900", section="peint"),
        LigneMontant("1180")]))                       # sans section
    ca.comptabiliser_depense("D1", "2026-02-10", "4000", compte_charge="6222",
                             libelle="Loyer rayon peinture", section="PEINT")
    ca.comptabiliser_paie(BulletinPaie("P1", J(2026, 2, 28), "E1", "Ali", "3000",
                                       section="QUINC"))
    ca.saisir("OD", J(2026, 2, 28), "Charge magasin",
              [Ligne("6288", debit=500, section="BKO"), Ligne("5711", credit=500)])
    r = ca.resultat_analytique("2026-01-01", "2026-12-31", axe="RAYON", detail=True)
    s = {x["section"]: x for x in r["sections"]}
    assert s["QUINC"]["produits"] == D(10000) and s["QUINC"]["charges"] == D(3000)
    assert s["QUINC"]["resultat"] == D(7000) and s["QUINC"]["marge_pct"] == D("70.0")
    assert s["PEINT"]["resultat"] == D(5000 - 4000)
    assert s["Non affecté"]["produits"] == D(1000)
    assert s["Non affecté"]["charges"] == D(500)        # section d'un autre axe
    assert [c["compte"] for c in s["QUINC"]["comptes"]] == ["6611", "7011"]
    # La somme des sections redonne le résultat comptable.
    assert r["total"]["resultat"] == ca.compte_de_resultat("2026")["resultat_net"]
    magasin = ca.resultat_analytique("2026-01-01", "2026-12-31", axe="MAGASIN")
    assert {x["section"] for x in magasin["sections"]} == {"BKO", "Non affecté"}
    assert ca.verifier_integrite()["ok"]


def test_section_conservee_par_la_contrepassation(ca):
    v = ca.comptabiliser_vente(Vente("V1", J(2026, 2, 1),
                                     [LigneMontant("1180", section="QUINC")]))
    ca.contrepasser(v["facture"]["id"], date_ecriture="2026-02-02")
    r = ca.resultat_analytique("2026-01-01", "2026-12-31")
    assert {x["section"]: x["resultat"] for x in r["sections"]}["QUINC"] == D(0)


def test_casse_de_section_normalisee(ca):
    """Régression : « peint » et « PEINT » produisaient deux lignes."""
    r = ca.comptabiliser_vente(Vente("V", J(2026, 2, 1), [
        LigneMontant("1180", section="peint"), LigneMontant("1180", section="PEINT")]))
    produits = [l for l in r["facture"]["lignes"] if l["compte"] == "7011"]
    assert [(l["section"], l["credit"]) for l in produits] == [("PEINT", D(2000))]
