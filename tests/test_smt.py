"""Système minimal de trésorerie : éligibilité, bilan et résultat SMT, livre
des recettes et dépenses."""

from decimal import Decimal

import pytest

from compta_ohada import (Achat, Ligne, LigneMontant, ParametreInvalide,
                          Reglement, Vente, eligible_smt)
from conftest import D, J


def test_seuils_article_13():
    assert eligible_smt("59999999", "negoce")
    assert not eligible_smt("60000000", "negoce")
    assert eligible_smt("39999999", "artisanat")
    assert not eligible_smt("30000000", "services")
    with pytest.raises(ParametreInvalide):
        eligible_smt(1, "agriculture")


def boutique(c):
    c.initialiser(exercice=2026, assujetti_tva=False)
    c.saisir("OD", J(2026, 1, 2), "Apport de l'exploitant",
             [Ligne("5711", debit="2000000"), Ligne("104", credit="2000000")])
    c.comptabiliser_achat(Achat("A1", J(2026, 1, 5), [LigneMontant("1200000")],
                                reglements=[Reglement("1200000")]))
    c.comptabiliser_vente(Vente("V1", J(2026, 2, 1), [LigneMontant("1500000")],
                                reglements=[Reglement("1500000")]))
    c.comptabiliser_vente(Vente("V2", J(2026, 3, 1), [LigneMontant("300000")],
                                tiers_ref="C1"))
    c.comptabiliser_depense("L1", "2026-03-31", "100000", compte_charge="6222",
                            libelle="Loyer")
    c.comptabiliser_virement_interne("VI", "2026-04-01", "500000", de="5711",
                                     vers="5211")
    c.comptabiliser_inventaire("INV", "2026-12-31", stock_initial=0,
                               stock_final="200000")


@pytest.fixture
def petit(engine):
    from compta_ohada import Comptabilite
    c = Comptabilite(engine, "TPE", poste="P1")
    boutique(c)
    return c


def test_etats_smt(petit):
    e = petit.etats_smt("2026", activite="negoce")
    assert e["equilibre"] and e["eligible_smt"] is True
    assert e["chiffre_affaires"] == D(1800000)
    cr = {l["code"]: l["montant"] for l in e["compte_de_resultat"]}
    assert cr["R1"] == D(1800000) and cr["RT"] == D(1800000)
    assert cr["D1"] == D(1200000) and cr["D2"] == D(-200000)
    assert cr["D3"] == D(100000)
    assert cr["RN"] == e["resultat"] == D(700000)
    assert e["resultat"] == petit.compte_de_resultat("2026")["resultat_net"]
    bilan = {l["code"]: l["montant"] for l in e["bilan"]}
    assert bilan["A2"] == D(200000) and bilan["A3"] == D(300000)
    assert bilan["A5"] == D(2200000)
    assert bilan["P1"] == D(2000000) and bilan["P2"] == D(700000)
    assert bilan["AT"] == bilan["PT"] == D(2700000)


def test_livre_recettes_depenses(petit):
    livre = petit.livre_recettes_depenses("2026-01-01", "2026-12-31")
    assert livre["solde_initial"] == D(0)
    assert livre["total_recettes"] == D(2000000 + 1500000)
    assert livre["total_depenses"] == D(1200000 + 100000)
    assert livre["solde_final"] == D(2200000)
    # le virement caisse -> banque n'est ni une recette ni une dépense
    assert all("Virement" not in m["libelle"] for m in livre["mouvements"])
    soldes = [m["solde"] for m in livre["mouvements"]]
    assert soldes[-1] == livre["solde_final"]
    natures = {n["nature"]: n for n in livre["par_nature"]}
    assert natures["Clients"]["recettes"] == D(1500000)
    caisse = petit.livre_recettes_depenses("2026-01-01", "2026-12-31",
                                           comptes_tresorerie=["5711"])
    assert caisse["solde_final"] == D(2000000 + 1500000 - 1200000 - 100000 - 500000)


def test_smt_apres_cloture(petit):
    petit.cloturer_exercice("2026")
    e27 = petit.etats_smt("2027", activite="negoce")
    assert e27["equilibre"]
    bilan = {l["code"]: l["montant"] for l in e27["bilan"]}
    assert bilan["P2"] == D(700000)          # résultat 2026 non encore affecté
    livre = petit.livre_recettes_depenses("2027-01-01", "2027-12-31")
    assert livre["solde_initial"] == D(2200000)
