"""Immobilisations : plans, dotations automatiques, cession, rebut, tableau."""

from datetime import date
from decimal import Decimal

import pytest

from compta_ohada import CompteInconnu, EcritureInvalide, ErreurComptable, Ligne
from compta_ohada.immobilisations import (_fin_de_plan, _jours_30_360,
                                          coefficient_degressif)
from conftest import D, J


def lignes(ecr):
    return [(l["compte"], l["debit"], l["credit"]) for l in ecr["lignes"]]


def ordinateur(compta, **kw):
    params = dict(valeur_origine="900000", date_acquisition="2026-06-01",
                  date_mise_en_service="2026-06-15", duree_mois=36,
                  contrepartie="481", tiers_ref="F2")
    params.update(kw)
    return compta.creer_immobilisation("info-001", "Ordinateur portable", "2442",
                                       **params)


def test_conventions_de_calcul():
    assert _jours_30_360(J(2026, 1, 1), J(2026, 12, 31)) == 360
    assert _jours_30_360(J(2026, 2, 1), J(2026, 2, 28)) == 30
    assert _jours_30_360(J(2026, 6, 15), J(2026, 12, 31)) == 196
    assert _fin_de_plan(J(2026, 6, 15), 36) == J(2029, 6, 14)
    assert _fin_de_plan(J(2026, 1, 31), 1) == J(2026, 2, 27)
    assert coefficient_degressif(36) == Decimal("1.5")
    assert coefficient_degressif(60) == Decimal("2")
    assert coefficient_degressif(120) == Decimal("2.5")
    with pytest.raises(EcritureInvalide):
        coefficient_degressif(24)


def test_creation_avec_acquisition(compta):
    i = ordinateur(compta)
    assert i["code"] == "INFO-001"
    assert i["compte_amort"] == "2844" and i["compte_dotation"] == "6813"
    assert lignes(i["acquisition"]) == [("2442", D(900000), D(0)),
                                        ("481", D(0), D(900000))]
    with pytest.raises(EcritureInvalide, match="existe déjà"):
        ordinateur(compta)


@pytest.mark.parametrize("kw,erreur", [
    ({"valeur_origine": "0"}, EcritureInvalide),
    ({"valeur_residuelle": "900000"}, EcritureInvalide),
    ({"date_mise_en_service": "2026-05-01"}, EcritureInvalide),
    ({"duree_mois": 0}, EcritureInvalide),
    ({"methode": "AUTRE"}, EcritureInvalide),
    ({"methode": "DEGRESSIF", "coefficient": "0.5"}, EcritureInvalide),
    ({"compte_amort": "6813"}, CompteInconnu),
    ({"compte_dotation": "2844"}, CompteInconnu),
    ({"amortissements_anterieurs": "1000000"}, EcritureInvalide),
])
def test_creation_controles(compta, kw, erreur):
    with pytest.raises(erreur):
        ordinateur(compta, **kw)


def test_compte_non_immobilisation_refuse(compta):
    with pytest.raises(CompteInconnu):
        compta.creer_immobilisation("X", "x", "6011", valeur_origine=1,
                                    date_acquisition="2026-01-01", duree_mois=12)
    t = compta.creer_immobilisation("TERRAIN", "Terrain ACI", "222",
                                    valeur_origine="20000000",
                                    date_acquisition="2026-01-10", duree_mois=0)
    assert t["compte_amort"] is None
    assert compta.plan_amortissement("TERRAIN") == []


def test_plan_lineaire(compta):
    ordinateur(compta)
    p = compta.plan_amortissement("INFO-001")
    assert [(a["exercice"], a["dotation"]) for a in p] == [
        (2026, D("163333.33")), (2027, D(300000)), (2028, D(300000)),
        (2029, D("136666.67"))]
    assert p[-1]["cumul"] == D(900000) and p[-1]["vnc"] == D(0)


def test_plan_degressif(compta):
    compta.creer_immobilisation("VEH", "Camionnette", "245",
                                valeur_origine="1000000",
                                date_acquisition="2026-04-10", duree_mois=60,
                                methode="DEGRESSIF")
    p = compta.plan_amortissement("VEH")
    assert [a["dotation"] for a in p] == [D(300000), D(280000), D(168000),
                                          D(112000), D(112000), D(28000)]
    assert compta.immobilisations(au="2026-12-31")[0]["coefficient"] == D(2)


def test_dotations_idempotentes_et_progressives(compta):
    ordinateur(compta)
    juin = compta.comptabiliser_dotations("2026-06-30")
    # 15/06 -> 30/06 : 16 jours sur 1 080
    assert lignes(juin) == [("6813", D("13333.33"), D(0)),
                            ("2844", D(0), D("13333.33"))]
    assert juin["lignes"][1]["tiers_ref"] == "INFO-001"
    assert compta.comptabiliser_dotations("2026-06-30")["id"] == juin["id"]
    dec = compta.comptabiliser_dotations("2026-12-31")
    assert lignes(dec) == [("6813", D(150000), D(0)), ("2844", D(0), D(150000))]
    assert compta.comptabiliser_dotations("2026-12-31")["id"] == dec["id"]
    assert compta.comptabiliser_dotations("2026-10-31") is None   # déjà couvert
    fiche = compta.immobilisations(au="2026-12-31")[0]
    assert fiche["amortissements_cumules"] == D("163333.33")
    assert fiche["vnc"] == D("736666.67")


def test_dotations_sur_plusieurs_exercices_et_fin_de_plan(compta):
    ordinateur(compta)
    compta.comptabiliser_dotations("2026-12-31")
    compta.cloturer_exercice("2026")
    compta.comptabiliser_dotations("2027-12-31")
    compta.cloturer_exercice("2027")
    compta.comptabiliser_dotations("2028-12-31")
    compta.cloturer_exercice("2028")
    compta.comptabiliser_dotations("2029-12-31")
    fiche = compta.immobilisations(au="2029-12-31")[0]
    assert fiche["amortissements_cumules"] == D(900000) and fiche["vnc"] == D(0)
    compta.cloturer_exercice("2029")
    assert compta.comptabiliser_dotations("2030-12-31") is None     # plan terminé
    assert compta.bilan("2029")["equilibre"]
    assert compta.verifier_integrite()["ok"]


def test_cession_avec_plus_value(compta):
    ordinateur(compta)
    compta.comptabiliser_dotations("2026-12-31")
    r = compta.ceder_immobilisation("INFO-001", "2026-12-31", prix="800000",
                                    compte_tresorerie="5711", tiers_ref="ACHETEUR")
    assert r["dotation"] is None
    assert lignes(r["sortie"]) == [("2844", D("163333.33"), D(0)),
                                   ("812", D("736666.67"), D(0)),
                                   ("2442", D(0), D(900000))]
    assert lignes(r["cession"]) == [("485", D(800000), D(0)),
                                    ("822", D(0), D(800000))]
    assert r["encaissement"] is not None
    assert r["plus_ou_moins_value"] == D("63333.33")
    assert compta.lignes_non_lettrees("485", tiers_ref="ACHETEUR") == []
    assert compta.immobilisations(au="2026-12-31") == []
    sortie = compta.immobilisations(au="2026-12-31", inclure_sorties=True)[0]
    assert sortie["statut"] == "CEDEE" and sortie["prix_cession"] == D(800000)
    with pytest.raises(ErreurComptable, match="déjà sortie"):
        compta.ceder_immobilisation("INFO-001", "2026-12-31", prix=1)
    cr = {l["code"]: l["montant"] for l in compta.compte_de_resultat("2026")["lignes"]}
    assert cr["TN"] == D(800000) and cr["RO"] == D("736666.67")
    assert compta.bilan("2026")["equilibre"]
    tft = compta.tableau_flux_tresorerie("2026")
    assert tft["ecart_controle"] == D(0)
    assert {l["code"]: l["montant"] for l in tft["lignes"]}["FI"] == D(800000)


def test_rebut_avec_dotation_complementaire(compta):
    ordinateur(compta)
    r = compta.mettre_au_rebut("INFO-001", "2026-09-30")
    # 15/06 -> 30/09 : 106 jours 30/360 sur 1 080
    assert lignes(r["dotation"]) == [("6813", D("88333.33"), D(0)),
                                     ("2844", D(0), D("88333.33"))]
    assert lignes(r["sortie"]) == [("2844", D("88333.33"), D(0)),
                                   ("812", D("811666.67"), D(0)),
                                   ("2442", D(0), D(900000))]
    assert r["cession"] is None and r["plus_ou_moins_value"] == D("-811666.67")
    assert compta.comptabiliser_dotations("2026-12-31") is None


def test_reprise_d_un_parc_existant(compta):
    """Client qui démarre sur le logiciel avec un véhicule déjà amorti."""
    compta.saisir("OD", J(2026, 1, 1), "Reprise du bilan d'ouverture", [
        Ligne("245", debit="6000000"), Ligne("2845", credit="2400000"),
        Ligne("101", credit="3600000")])
    compta.creer_immobilisation("VEH-01", "Toyota Hilux", "245",
                                valeur_origine="6000000",
                                date_acquisition="2024-01-01", duree_mois=60,
                                amortissements_anterieurs="2400000")
    d = compta.comptabiliser_dotations("2026-12-31")
    assert lignes(d) == [("6813", D(1200000), D(0)), ("2845", D(0), D(1200000))]
    fiche = compta.immobilisations(au="2026-12-31")[0]
    assert fiche["vnc"] == D(2400000)
    r = compta.ceder_immobilisation("VEH-01", "2026-12-31", prix="2500000")
    assert ("2845", D(3600000), D(0)) in lignes(r["sortie"])
    soldes = {l["compte"]: l for l in compta.balance("2026-01-01", "2026-12-31")["lignes"]}
    assert soldes["2845"]["solde_debit"] == soldes["2845"]["solde_credit"] == D(0)
    assert soldes["245"]["solde_debit"] == soldes["245"]["solde_credit"] == D(0)


def test_tableau_des_immobilisations(compta):
    ordinateur(compta)
    compta.creer_immobilisation("BUREAU", "Bureau direction", "2444",
                                valeur_origine="300000",
                                date_acquisition="2026-01-01", duree_mois=120,
                                contrepartie="5211")
    compta.comptabiliser_dotations("2026-12-31")
    t26 = compta.tableau_immobilisations("2026")
    assert t26["totaux"]["entrees"] == D(1200000)
    assert t26["totaux"]["dotations"] == D("163333.33") + D(30000)
    compta.cloturer_exercice("2026")
    compta.ceder_immobilisation("BUREAU", "2027-06-30", prix="250000")
    compta.comptabiliser_dotations("2027-12-31")
    t27 = {l["code"]: l for l in compta.tableau_immobilisations("2027")["lignes"]}
    b = t27["BUREAU"]
    assert b["brut_debut"] == D(300000) and b["sorties"] == D(300000)
    assert b["amort_debut"] == D(30000) and b["dotations"] == D(15000)
    assert b["reprises"] == D(45000) and b["amort_fin"] == D(0) and b["vnc"] == D(0)
    o = t27["INFO-001"]
    assert o["amort_fin"] == D("463333.33") and o["vnc"] == D("436666.67")
    assert compta.bilan("2027")["equilibre"]


def test_cloture_signale_les_dotations_oubliees(compta):
    ordinateur(compta)
    cl = compta.cloturer_exercice("2026")
    assert cl["immobilisations_non_dotees"] == ["INFO-001"]
