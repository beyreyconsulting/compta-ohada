"""États (balance, grand livre, journaux, tiers, TVA), états financiers et clôture."""

import pytest
from sqlalchemy import text

from compta_ohada import (Achat, BulletinPaie, ChargePatronale, ErreurComptable,
                          ExerciceClos, ExerciceIntrouvable, Ligne, LigneMontant,
                          Reglement, Retenue, Vente)
from conftest import D, J


def activite_2026(c):
    """Une année d'un petit commerce : capital, achats, ventes, paie, immobilisation."""
    c.saisir("OD", J(2026, 1, 2), "Apport en capital",
             [Ligne("5211", debit="5000000"), Ligne("101", credit="5000000")])
    c.comptabiliser_achat(Achat("A1", J(2026, 1, 5), [LigneMontant("2000000")],
                                tiers_ref="F1", tiers_nom="Grossiste"))
    c.comptabiliser_reglement_fournisseur("PF1", "2026-01-20", "2360000",
                                          achat_source_id="A1", tiers_ref="F1",
                                          compte="5211")
    c.comptabiliser_vente(Vente("V1", J(2026, 2, 1), [LigneMontant("1770000")],
                                reglements=[Reglement("1770000")]))
    c.comptabiliser_vente(Vente("V2", J(2026, 11, 15), [LigneMontant("590000")],
                                tiers_ref="C1", tiers_nom="Awa",
                                date_echeance=J(2026, 12, 15)))
    c.comptabiliser_paie(BulletinPaie(
        "BP1", J(2026, 2, 28), "E1", "Ali", "200000",
        retenues=[Retenue("INPS", "7200", "431")],
        charges_patronales=[ChargePatronale("INPS", "36000")]))
    c.comptabiliser_paiement_salaire("PS1", "2026-03-01", "192800",
                                     salarie_ref="E1", bulletin_source_id="BP1")
    c.comptabiliser_depense("D1", "2026-03-31", "150000", compte_charge="6222",
                            libelle="Loyer")
    c.saisir("OD", J(2026, 6, 1), "Ordinateur",
             [Ligne("2442", debit="900000"), Ligne("481", credit="900000",
                                                   tiers_ref="F2")])
    c.saisir("BQ", J(2026, 6, 20), "Paiement ordinateur",
             [Ligne("481", debit="900000", tiers_ref="F2"),
              Ligne("5211", credit="900000")])
    c.saisir("OD", J(2026, 12, 31), "Dotation aux amortissements",
             [Ligne("6813", debit="150000"), Ligne("2844", credit="150000")])
    c.comptabiliser_inventaire("INV26", "2026-12-31", stock_initial=0,
                               stock_final="800000")
    c.comptabiliser_virement_interne("VI1", "2026-12-30", "1000000",
                                     de="5711", vers="5211")


# ---------------------------------------------------------------------------
# Balance, grand livre, journaux
# ---------------------------------------------------------------------------

def test_balance_equilibree_six_colonnes(compta):
    activite_2026(compta)
    b = compta.balance("2026-01-01", "2026-12-31")
    assert b["equilibree"]
    t = b["totaux"]
    assert t["debit"] == t["credit"] and t["solde_debit"] == t["solde_credit"]
    par_compte = {l["compte"]: l for l in b["lignes"]}
    assert par_compte["101"]["solde_credit"] == D(5000000)
    assert par_compte["2442"]["solde_debit"] == D(900000)
    rubriques = compta.balance("2026-01-01", "2026-12-31", niveau=2)
    assert rubriques["equilibree"]
    assert {l["compte"] for l in rubriques["lignes"]} >= {"10", "24", "52", "70"}
    classe7 = compta.balance("2026-01-01", "2026-12-31", classes=(7,))
    assert all(l["compte"].startswith("7") for l in classe7["lignes"])


def test_balance_periode_avec_ouverture(compta):
    activite_2026(compta)
    b = compta.balance("2026-06-01", "2026-06-30")
    assert b["equilibree"]
    ligne = {l["compte"]: l for l in b["lignes"]}["5211"]
    assert ligne["ouverture_debit"] == D(5000000 - 2360000)
    assert ligne["credit"] == D(900000)


def test_balance_hors_exercice(compta):
    with pytest.raises(ExerciceIntrouvable):
        compta.balance("2026-06-01", "2027-02-01")
    with pytest.raises(ExerciceIntrouvable):
        compta.balance("2025-01-01", "2025-12-31")


def test_grand_livre(compta):
    activite_2026(compta)
    gl = compta.grand_livre("2026-01-01", "2026-12-31", comptes=["5211"])
    assert len(gl) == 1
    compte = gl[0]
    soldes = [m["solde"] for m in compte["mouvements"]]
    assert soldes[0] == D(5000000)
    assert compte["solde"] == soldes[-1] == compte["total_debit"] - compte["total_credit"]
    tiers = compta.grand_livre("2026-01-01", "2026-12-31", prefixe="4111",
                               tiers_ref="C1")
    assert tiers[0]["solde"] == D(590000)


def test_journal_et_centralisateur(compta):
    activite_2026(compta)
    j = compta.journal("VT", "2026-01-01", "2026-12-31")
    assert j["equilibre"] and len(j["ecritures"]) == 2
    assert j["total_debit"] == D(1770000 + 590000)
    centr = compta.journal_centralisateur("2026-01-01", "2026-12-31")
    assert all(l["debit"] == l["credit"] for l in centr)
    assert {"journal": "VT", "mois": "2026-02", "debit": D(1770000),
            "credit": D(1770000)} in centr


def test_balance_tiers_et_agee(compta):
    activite_2026(compta)
    bt = compta.balance_tiers("411", "2026-12-31")
    assert bt == [{"tiers_ref": "C1", "tiers_nom": "Awa", "debit": D(590000),
                   "credit": D(0), "solde": D(590000)}]
    compta.comptabiliser_reglement_client("RC1", "2026-12-20", "90000",
                                          vente_source_id="V2", tiers_ref="C1")
    agee = compta.balance_agee("411", "2026-12-31")
    assert agee[0]["solde"] == D(500000)
    assert agee[0]["0_30"] == D(500000)          # échéance 15/12, 16 jours
    # Au 14/12 le règlement n'existe pas encore et la facture n'est pas échue.
    avant = compta.balance_agee("411", "2026-12-14")
    assert avant[0]["non_echu"] == D(590000)


# ---------------------------------------------------------------------------
# TVA
# ---------------------------------------------------------------------------

def test_declaration_et_liquidation_tva(compta):
    activite_2026(compta)
    d = compta.declaration_tva("2026-01-01", "2026-03-31")
    assert d["tva_collectee"] == D(270000)
    assert d["tva_deductible"] == D(360000)
    assert d["tva_due"] == D(0) and d["credit_a_reporter"] == D(90000)
    detail = {x["code"]: x for x in d["detail"]}
    assert detail["TVA18"]["base"] == D(1500000)
    assert detail["TVA18-ACH"]["base"] == D(2000000)
    liq = compta.liquider_tva("2026-01-01", "2026-03-31")
    assert ("4449", D(90000), D(0)) in [(l["compte"], l["debit"], l["credit"])
                                        for l in liq["lignes"]]
    assert compta.liquider_tva("2026-01-01", "2026-03-31")["id"] == liq["id"]
    # Trimestre suivant : le crédit antérieur est imputé.
    d4 = compta.declaration_tva("2026-04-01", "2026-12-31")
    assert d4["credit_anterieur"] == D(90000)
    assert d4["tva_collectee"] == D(90000)
    assert d4["tva_due"] == D(0) and d4["credit_a_reporter"] == D(0)
    liq4 = compta.liquider_tva("2026-04-01", "2026-12-31")
    soldes = {l["compte"]: l for l in compta.balance("2026-01-01", "2026-12-31")["lignes"]}
    for compte in ("4431", "4452", "4449"):
        assert soldes[compte]["solde_debit"] == soldes[compte]["solde_credit"]
    assert liq4 is not None


# ---------------------------------------------------------------------------
# États financiers
# ---------------------------------------------------------------------------

def test_bilan_compte_de_resultat_tft(compta):
    activite_2026(compta)
    cr = compta.compte_de_resultat("2026")
    v = {l["code"]: l["montant"] for l in cr["lignes"]}
    assert v["TA"] == D(2000000)                 # 1 500 000 + 500 000 HT
    assert v["RA"] == D(2000000) and v["RB"] == D(-800000)
    assert v["XA"] == D(800000)
    assert v["RH"] == D(150000) and v["RK"] == D(236000)
    assert v["RL"] == D(150000)
    attendu = D(800000 - 150000 - 236000 - 150000)
    assert cr["resultat_net"] == v["XI"] == attendu

    bilan = compta.bilan("2026")
    assert bilan["equilibre"] and bilan["comptes_non_classes"] == []
    assert bilan["resultat"] == attendu
    actif = {l["code"]: l for l in bilan["actif"]}
    assert actif["AM"]["brut"] == D(900000) and actif["AM"]["amort"] == D(150000)
    assert actif["AM"]["net"] == D(750000)
    assert actif["BB"]["net"] == D(800000)
    assert actif["BI"]["net"] == D(590000)
    # TVA récupérable (4452) à l'actif, TVA collectée (4431) au passif : pas
    # de compensation tant que la TVA n'est pas liquidée.
    assert actif["BJ"]["net"] == D(360000)
    assert {l["code"]: l["net"] for l in bilan["passif"]}["DK"] >= D(360000)
    codes = [l["code"] for l in bilan["actif"]]
    assert codes[:5] == ["AD", "AE", "AF", "AG", "AH"] and codes[-1] == "BZ"

    tft = compta.tableau_flux_tresorerie("2026")
    assert tft["ecart_controle"] == D(0)
    assert tft["tresorerie_ouverture"] == D(0)
    assert tft["tresorerie_cloture"] == actif["BT"]["net"]
    lignes = {l["code"]: l["montant"] for l in tft["lignes"]}
    assert lignes["FA"] == attendu + D(150000)          # CAFG = résultat + dotation
    assert lignes["FG"] == D(-900000)
    assert lignes["FK"] == D(5000000)
    assert lignes["FC"] == D(-800000)


def test_bilan_non_classe_signale(compta):
    compta.saisir("OD", J(2026, 1, 2), "x",
                  [Ligne("5211", debit=1), Ligne("101", credit=1)])
    assert compta.bilan("2026")["comptes_non_classes"] == []


# ---------------------------------------------------------------------------
# Clôture
# ---------------------------------------------------------------------------

def test_cloture_complete(compta):
    activite_2026(compta)
    compta.verrouiller_jusqu_au("2026-12-31")     # déclarations déposées
    resultat = compta.compte_de_resultat("2026")["resultat_net"]
    cl = compta.cloturer_exercice("2026")
    assert cl["resultat"] == resultat
    assert cl["exercice_suivant"] == "2027"
    assert cl["ecriture_resultat"]["type"] == "CLOTURE"
    assert ("131", D(0), resultat) in [(l["compte"], l["debit"], l["credit"])
                                       for l in cl["ecriture_resultat"]["lignes"]]
    an = cl["a_nouveaux"]
    assert an["journal"] == "AN" and an["date"] == J(2027, 1, 1)
    clients = [l for l in an["lignes"] if l["compte"] == "4111"]
    assert clients == [dict(clients[0], tiers_ref="C1", debit=D(590000))]
    assert clients[0]["date_echeance"] == J(2026, 12, 15)
    assert [e["statut"] for e in compta.exercices()] == ["CLOTURE", "OUVERT"]

    # Après clôture : CR de N intact, bilan d'ouverture N+1 = bilan de clôture N.
    assert compta.compte_de_resultat("2026")["resultat_net"] == resultat
    b26, b27 = compta.bilan("2026"), compta.bilan("2027")
    assert b26["equilibre"] and b27["equilibre"]
    assert b27["total_actif"] == b26["total_actif"]
    assert b27["resultat"] == resultat               # encore en 131, non affecté
    assert compta.balance("2027-01-01", "2027-12-31")["equilibree"]
    assert compta.tableau_flux_tresorerie("2027")["ecart_controle"] == D(0)
    cr27 = {l["code"]: l for l in compta.compte_de_resultat("2027")["lignes"]}
    assert cr27["TA"]["montant"] == D(0) and cr27["TA"]["montant_n1"] == D(2000000)
    assert compta.verifier_integrite()["ok"]

    # La créance reprise en à-nouveau se lettre avec son règlement de N+1.
    r = compta.comptabiliser_reglement_client("RC", "2027-01-10", "590000",
                                              vente_source_id="V2", tiers_ref="C1")
    assert r["lettrage"] is not None
    assert compta.balance_agee("411", "2027-01-31") == []

    # Affectation : réserve légale 10 %, dividendes, report à nouveau.
    aff = compta.affecter_resultat("2026", "2027-06-30", reserve_legale="26400",
                                   dividendes="100000")
    l = {(x["compte"]): (x["debit"], x["credit"]) for x in aff["lignes"]}
    assert l["131"] == (resultat, D(0))
    assert l["111"] == (D(0), D(26400)) and l["465"] == (D(0), D(100000))
    assert l["121"] == (D(0), resultat - D(126400))
    assert compta.bilan("2027")["equilibre"]
    with pytest.raises(Exception):
        compta.affecter_resultat("2026", "2027-06-30", reserve_legale=resultat + 1)


def test_cloture_perte(compta):
    compta.saisir("OD", J(2026, 1, 2), "Apport", [Ligne("5711", debit=1000),
                                                  Ligne("101", credit=1000)])
    compta.comptabiliser_depense("D", "2026-02-01", "300",
                                 compte_charge="6222", libelle="Loyer")
    cl = compta.cloturer_exercice("2026")
    assert cl["resultat"] == D(-300)
    assert ("139", D(300), D(0)) in [(l["compte"], l["debit"], l["credit"])
                                     for l in cl["ecriture_resultat"]["lignes"]]
    aff = compta.affecter_resultat("2026", "2027-05-01")
    assert [(l["compte"], l["debit"], l["credit"]) for l in aff["lignes"]] == [
        ("129", D(300), D(0)), ("139", D(0), D(300))]
    with pytest.raises(ExerciceClos):
        compta.affecter_resultat("2025", "2027-05-01")


def test_cloture_refusee(compta):
    compta.saisir("OD", J(2026, 3, 1), "b", [Ligne("5711", debit=1),
                                             Ligne("101", credit=1)], valider=False)
    with pytest.raises(ExerciceClos, match="brouillard"):
        compta.cloturer_exercice("2026")
    with pytest.raises(ExerciceClos):
        compta.cloturer_exercice("1999")


def test_cloture_refusee_si_precedent_ouvert(compta):
    compta.ouvrir_exercice("2027", "2027-01-01", "2027-12-31")
    with pytest.raises(ExerciceClos, match="précédent"):
        compta.cloturer_exercice("2027")


def test_cloture_refusee_si_integrite_rompue(engine, compta):
    compta.saisir("OD", J(2026, 3, 1), "x", [Ligne("5711", debit=100),
                                             Ligne("101", credit=100)])
    with engine.begin() as cx:
        cx.execute(text("UPDATE compta_ecritures SET libelle = 'falsifié'"))
    with pytest.raises(ErreurComptable, match="intégrité"):
        compta.cloturer_exercice("2026")


def test_double_cloture_refusee(compta):
    compta.cloturer_exercice("2026")
    with pytest.raises(ExerciceClos, match="déjà"):
        compta.cloturer_exercice("2026")


def test_etats_avant_cloture_du_precedent(compta):
    """N+1 commencé avant la clôture de N : ouverture reconstituée à blanc."""
    activite_2026(compta)
    compta.ouvrir_exercice("2027", "2027-01-01", "2027-12-31")
    compta.comptabiliser_vente(Vente("V27", J(2027, 1, 5), [LigneMontant("118000")],
                                     reglements=[Reglement("118000")]))
    b27 = compta.bilan("2027")
    assert b27["equilibre"]
    passif = {l["code"]: l["net"] for l in b27["passif"]}
    resultat_2026 = compta.compte_de_resultat("2026")["resultat_net"]
    assert passif["CJ"] == resultat_2026 + D(100000)   # 130 + résultat 2027
    assert compta.balance("2027-01-01", "2027-12-31")["equilibree"]
    assert compta.tableau_flux_tresorerie("2027")["ecart_controle"] == D(0)
