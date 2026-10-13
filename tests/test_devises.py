"""Opérations en devises : conversion, écarts réalisés, réévaluation de
clôture, à-nouveaux en devise et extourne à l'ouverture."""

import pytest

from compta_ohada import (Achat, EcritureInvalide, Ligne, LigneMontant,
                          ParametreInvalide, Reglement, Vente)
from conftest import D, J


def lignes(ecr):
    return [(l["compte"], l["debit"], l["credit"]) for l in ecr["lignes"]]


@pytest.fixture
def cd(compta):
    compta.definir_cours("USD", "2026-03-01", "600")
    compta.definir_cours("USD", "2026-04-01", "620")
    compta.definir_cours("USD", "2026-12-31", "580")
    compta.definir_cours("USD", "2027-02-01", "590")
    return compta


def test_parite_fixe_euro_installee(compta):
    from decimal import Decimal
    assert compta.cours_du("EUR", "2026-06-15") == Decimal("655.957")
    assert compta.cours_du("eur", "2030-01-01") == Decimal("655.957")


def test_cours(cd):
    assert cd.cours_du("USD", "2026-03-15") == D(600)
    assert cd.cours_du("USD", "2026-04-01") == D(620)
    cd.definir_cours("USD", "2026-04-01", "621.5")          # correction du jour
    assert cd.cours_du("USD", "2026-04-02") == D("621.5")
    with pytest.raises(ParametreInvalide):
        cd.cours_du("USD", "2026-01-01")                    # aucun cours avant
    with pytest.raises(ParametreInvalide):
        cd.definir_cours("XOF", "2026-01-01", "1")          # monnaie de tenue
    with pytest.raises(ParametreInvalide):
        cd.definir_cours("USD", "2026-01-01", "0")
    with pytest.raises(EcritureInvalide):
        cd.definir_cours("DOLLAR", "2026-01-01", "600")


def test_vente_en_euros_avec_tva(compta):
    r = compta.comptabiliser_vente(Vente("VE", J(2026, 2, 1), [LigneMontant("100")],
                                         tiers_ref="C-EU", devise="EUR"))
    f = r["facture"]
    assert lignes(f) == [("4111", D("65595.70"), D(0)),
                         ("7011", D(0), D("55592.36")),
                         ("4431", D(0), D("10003.34"))]
    client = f["lignes"][0]
    assert client["devise"] == "EUR" and client["montant_devise"] == D(100)
    tva = f["lignes"][2]
    assert tva["base_taxe"] == D("55592.36")


def test_vente_dans_la_monnaie_de_tenue_ignore_la_devise(compta):
    r = compta.comptabiliser_vente(Vente("V", J(2026, 2, 1), [LigneMontant("1180")],
                                         devise="XOF"))
    assert r["facture"]["lignes"][0]["devise"] is None


def test_reglement_complet_gain_de_change(cd):
    cd.comptabiliser_vente(Vente("VU", J(2026, 3, 2), [LigneMontant("1000", taxe="")],
                                 tiers_ref="C-US", devise="USD"))
    r = cd.comptabiliser_reglement_client("RU", "2026-04-02", "1000",
                                          vente_source_id="VU", tiers_ref="C-US",
                                          compte="5211", devise="USD")
    assert lignes(r["reglement"]) == [("5211", D(620000), D(0)),
                                      ("4111", D(0), D(600000)),
                                      ("756", D(0), D(20000))]
    assert r["lettrage"] is not None


def test_reglement_partiel_perte_de_change(cd):
    cd.comptabiliser_achat(Achat("AU", J(2026, 3, 2), [LigneMontant("500", taxe="")],
                                 tiers_ref="F-US", devise="USD"))
    r = cd.comptabiliser_reglement_fournisseur("PU1", "2026-04-02", "200",
                                               achat_source_id="AU", tiers_ref="F-US",
                                               compte="5211", devise="USD")
    assert lignes(r["reglement"]) == [("5211", D(0), D(124000)),
                                      ("4011", D(120000), D(0)),
                                      ("656", D(4000), D(0))]
    assert r["lettrage"] is None
    # Cours imposé au règlement : solde exact au cours historique.
    r2 = cd.comptabiliser_reglement_fournisseur("PU2", "2026-04-03", "300",
                                                achat_source_id="AU",
                                                tiers_ref="F-US", compte="5211",
                                                devise="USD", cours="600")
    assert lignes(r2["reglement"]) == [("5211", D(0), D(180000)),
                                       ("4011", D(180000), D(0))]
    assert r2["lettrage"] is not None


def test_reglements_immediats_en_devise(cd):
    r = cd.comptabiliser_vente(Vente(
        "VI", J(2026, 3, 2), [LigneMontant("100", taxe="")], devise="USD",
        reglements=[Reglement("60", compte="5211"),
                    Reglement("40", compte="5211", date=J(2026, 4, 2))]))
    assert lignes(r["reglements"][0])[:2] == [("5211", D(36000), D(0)),
                                              ("4111", D(0), D(36000))]
    assert ("756", D(0), D(800)) in lignes(r["reglements"][1])
    assert r["lettrage"] is not None
    with pytest.raises(EcritureInvalide, match="supérieurs"):
        cd.comptabiliser_vente(Vente("VX", J(2026, 3, 2), [LigneMontant("100", taxe="")],
                                     devise="USD", reglements=[Reglement("101")]))


def test_compte_tenu_en_devise(cd):
    cd.creer_compte("52151", "Banque Ecobank USD", devise="USD")
    assert cd.comptes(prefixe="52151")[0]["devise"] == "USD"
    cd.creer_journal("BUS", "Banque USD", "TRESORERIE", compte_tresorerie="52151")
    with pytest.raises(EcritureInvalide, match="tenu en USD"):
        cd.saisir("BUS", J(2026, 3, 5), "x", [Ligne("52151", debit=1000),
                                              Ligne("101", credit=1000)])
    with pytest.raises(EcritureInvalide, match="pas en EUR"):
        cd.saisir("BUS", J(2026, 3, 5), "x",
                  [Ligne("52151", debit=1000, devise="EUR", montant_devise=1),
                   Ligne("101", credit=1000)])
    with pytest.raises(EcritureInvalide, match="sans devise"):
        cd.saisir("OD", J(2026, 3, 5), "x", [Ligne("5211", debit=1, montant_devise=1),
                                             Ligne("101", credit=1)])
    with pytest.raises(EcritureInvalide, match="monnaie de tenue"):
        cd.saisir("OD", J(2026, 3, 5), "x",
                  [Ligne("5211", debit=1, devise="XOF", montant_devise=1),
                   Ligne("101", credit=1)])
    from compta_ohada import CompteInconnu
    with pytest.raises(CompteInconnu):
        cd.creer_compte("52152", "Banque XOF", devise="XOF")
    cd.comptabiliser_vente(Vente("VB", J(2026, 3, 2), [LigneMontant("100", taxe="")],
                                 devise="USD", reglements=[Reglement("100",
                                                                     compte="52151")]))
    tresorerie = [l for l in cd.ecritures(journal="BUS")[0]["lignes"]
                  if l["compte"] == "52151"][0]
    assert tresorerie["devise"] == "USD" and tresorerie["montant_devise"] == D(100)


def test_reevaluation_et_cycle_de_cloture(cd):
    cd.creer_compte("52151", "Banque Ecobank USD", devise="USD")
    cd.creer_journal("BUS", "Banque USD", "TRESORERIE", compte_tresorerie="52151")
    cd.saisir("OD", J(2026, 1, 2), "Capital", [Ligne("5211", debit="5000000"),
                                               Ligne("101", credit="5000000")])
    # Dette fournisseur 300 USD restante, créance client 100 USD, 1 000 USD en banque.
    cd.comptabiliser_achat(Achat("AU", J(2026, 3, 2), [LigneMontant("500", taxe="")],
                                 tiers_ref="F-US", devise="USD"))
    cd.comptabiliser_reglement_fournisseur("PU1", "2026-04-02", "200",
                                           achat_source_id="AU", tiers_ref="F-US",
                                           compte="5211", devise="USD")
    cd.comptabiliser_vente(Vente("VU", J(2026, 3, 2), [LigneMontant("100", taxe="")],
                                 tiers_ref="C-US", devise="USD"))
    cd.comptabiliser_vente(Vente("VB", J(2026, 3, 2), [LigneMontant("1000", taxe="")],
                                 devise="USD",
                                 reglements=[Reglement("1000", compte="52151")]))

    reev = cd.reevaluer_devises("2026-12-31")
    ecarts = lignes(reev["ecarts"])
    assert ("4011", D(6000), D(0)) in ecarts and ("479", D(0), D(6000)) in ecarts
    assert ("478", D(2000), D(0)) in ecarts and ("4111", D(0), D(2000)) in ecarts
    assert lignes(reev["tresorerie"]) == [("676", D(20000), D(0)),
                                          ("52151", D(0), D(20000))]
    assert lignes(reev["provision"]) == [("697", D(2000), D(0)),
                                         ("194", D(0), D(2000))]
    assert cd.reevaluer_devises("2026-12-31")["ecarts"]["id"] == reev["ecarts"]["id"]
    bilan = cd.bilan("2026")
    assert bilan["equilibre"]
    actif = {l["code"]: l["net"] for l in bilan["actif"]}
    passif = {l["code"]: l["net"] for l in bilan["passif"]}
    assert actif["BU"] == D(2000) and passif["DV"] == D(6000)
    assert cd.tableau_flux_tresorerie("2026")["ecart_controle"] == D(0)

    cd.verrouiller_jusqu_au("2026-12-31")
    cl = cd.cloturer_exercice("2026")
    extourne = lignes(cl["extourne_ecarts_conversion"])
    assert ("4011", D(0), D(6000)) in extourne and ("478", D(0), D(2000)) in extourne
    an = cl["a_nouveaux"]["lignes"]
    dette = [l for l in an if l["compte"] == "4011" and l["tiers_ref"] == "F-US"][0]
    assert dette["devise"] == "USD" and dette["montant_devise"] == D(300)
    assert dette["credit"] == D(174000)            # valeur au cours de clôture
    banque = [l for l in an if l["compte"] == "52151"][0]
    assert banque["montant_devise"] == D(1000) and banque["debit"] == D(580000)
    assert cd.bilan("2027")["equilibre"]

    # En 2027 : la dette repart au cours historique (180 000), payée à 590.
    r = cd.comptabiliser_reglement_fournisseur("PU2", "2027-02-01", "300",
                                               achat_source_id="AU", tiers_ref="F-US",
                                               compte="5211", devise="USD")
    assert lignes(r["reglement"]) == [("5211", D(0), D(177000)),
                                      ("4011", D(180000), D(0)),
                                      ("756", D(0), D(3000))]
    assert r["lettrage"] is not None
    assert cd.lignes_non_lettrees("4011", tiers_ref="F-US") == []
    assert cd.verifier_integrite()["ok"]
    assert cd.balance("2027-01-01", "2027-12-31")["equilibree"]


def test_cloture_reevalue_automatiquement(cd):
    cd.comptabiliser_vente(Vente("VU", J(2026, 3, 2), [LigneMontant("100", taxe="")],
                                 tiers_ref="C-US", devise="USD"))
    cd.verrouiller_jusqu_au("2026-12-31")
    cl = cd.cloturer_exercice("2026")
    assert cl["extourne_ecarts_conversion"] is not None
    cr = {l["code"]: l["montant"] for l in cd.compte_de_resultat("2026")["lignes"]}
    assert cr["RN"] == D(2000)                     # dotation à la provision
    assert cd.bilan("2026")["equilibre"] and cd.bilan("2027")["equilibre"]


def test_contrepassation_conserve_la_devise(cd):
    r = cd.comptabiliser_vente(Vente("VU", J(2026, 3, 2), [LigneMontant("10", taxe="")],
                                     tiers_ref="C-US", devise="USD"))
    inv = cd.contrepasser(r["facture"]["id"], date_ecriture="2026-03-03")
    assert inv["lignes"][0]["devise"] == "USD"
    assert inv["lignes"][0]["montant_devise"] == D(10)
    assert cd.verifier_integrite()["ok"]


def test_reglement_dans_une_autre_devise_que_la_facture_refuse(cd):
    cd.comptabiliser_vente(Vente("VU", J(2026, 3, 2), [LigneMontant("100", taxe="")],
                                 tiers_ref="C-US", devise="USD"))
    with pytest.raises(EcritureInvalide, match="en USD"):
        cd.comptabiliser_reglement_client("R", "2026-04-02", "60000",
                                          vente_source_id="VU", tiers_ref="C-US")
    cd.comptabiliser_vente(Vente("VX", J(2026, 3, 2), [LigneMontant("1000", taxe="")],
                                 tiers_ref="C-X"))
    with pytest.raises(EcritureInvalide, match="monnaie de tenue"):
        cd.comptabiliser_reglement_client("R2", "2026-04-02", "10",
                                          vente_source_id="VX", tiers_ref="C-X",
                                          devise="USD")
