"""Génération automatique des écritures depuis les événements métier."""

from decimal import Decimal

import pytest

from compta_ohada import (Achat, BulletinPaie, ChargePatronale, Comptabilite,
                          EcritureInvalide, JournalInconnu, LigneMontant,
                          ParametreInvalide, Reglement, Retenue, Vente)
from compta_ohada.taxes import _repartir, ventiler
from conftest import D, J


def lignes(ecr):
    return [(l["compte"], l["debit"], l["credit"]) for l in ecr["lignes"]]


def soldes(compta, fin="2026-12-31"):
    return {l["compte"]: l["solde_debit"] - l["solde_credit"]
            for l in compta.balance("2026-01-01", fin)["lignes"]}


# ---------------------------------------------------------------------------
# Ventilation TVA
# ---------------------------------------------------------------------------

def test_ventilation_ttc_alignee_sur_la_facture_imprimee():
    """1 000 TTC à 18 % : HT = 847,46 ; TVA = 152,54 (calcul sur le total)."""
    v, t = ventiler([("7011", "TVA18", D(1000))], {"TVA18": (D(18), "4431")},
                    prix_ttc=True)
    assert v[0].ht == D("847.46") and v[0].ttc == D(1000)
    assert t[0].base == D("847.46") and t[0].tva == D("152.54")


def test_ventilation_repartit_la_tva_sans_perdre_un_centime():
    montants = [D("333.33"), D("333.33"), D("333.34")]
    v, t = ventiler([("7011", "TVA18", m) for m in montants],
                    {"TVA18": (D(18), "4431")}, prix_ttc=True)
    assert sum(x.ht for x in v) + t[0].tva == D(1000)
    assert sum(x.ttc for x in v) == D(1000)


def test_ventilation_ht():
    v, t = ventiler([("6011", "A", D("100.05"))], {"A": (D(18), "4452")}, prix_ttc=False)
    assert t[0].tva == D("18.01") and v[0].ttc == D("118.06")


def test_ventilation_remise_superieure_refusee():
    with pytest.raises(EcritureInvalide):
        ventiler([("7011", "T", D(100)), ("7011", "T", D(-150))],
                 {"T": (D(18), "4431")}, prix_ttc=True)


def test_repartir():
    assert _repartir(D("0.03"), [D(1), D(1), D(1)]) == [D("0.01")] * 3
    assert sum(_repartir(D("10.00"), [D(3), D(3), D(3)])) == D(10)
    assert _repartir(D(0), [D(0), D(0)]) == [D(0), D(0)]


# ---------------------------------------------------------------------------
# Ventes
# ---------------------------------------------------------------------------

def test_vente_comptant(compta):
    r = compta.comptabiliser_vente(Vente(
        "V1", J(2026, 2, 1), [LigneMontant("1000")], reference="T-0001",
        reglements=[Reglement("1000", compte="5711")]))
    f = r["facture"]
    assert f["journal"] == "VT" and f["reference"] == "T-0001"
    assert lignes(f) == [("4111", D(1000), D(0)), ("7011", D(0), D("847.46")),
                         ("4431", D(0), D("152.54"))]
    assert f["lignes"][0]["tiers_ref"] == "CLIENTS-DIVERS"
    assert r["reglements"][0]["journal"] == "CA"
    assert r["lettrage"] is not None
    s = soldes(compta)
    assert s["5711"] == D(1000) and s.get("4111", D(0)) == D(0)


def test_vente_idempotente(compta):
    v = Vente("V1", J(2026, 2, 1), [LigneMontant("1000")],
              reglements=[Reglement("600"), Reglement("400", compte="552")])
    a = compta.comptabiliser_vente(v)
    b = compta.comptabiliser_vente(v)
    assert a["facture"]["id"] == b["facture"]["id"]
    assert [x["id"] for x in a["reglements"]] == [x["id"] for x in b["reglements"]]
    assert len(compta.ecritures()) == 3
    assert b["reglements"][1]["journal"] == "MM"


def test_vente_multi_taux_et_remise(compta):
    compta.creer_taxe("TVA5", "TVA 5 %", "5", "VENTE", "4431")
    r = compta.comptabiliser_vente(Vente(
        "V2", J(2026, 2, 1), [
            LigneMontant("1180"), LigneMontant("-118", libelle="Remise"),
            LigneMontant("525", compte="706", taxe="TVA5"),
            LigneMontant("200", taxe=""),            # hors champ
        ], tiers_ref="C1", tiers_nom="Moussa"))
    f = r["facture"]
    assert f["total"] == D(1787)
    detail = {(l["compte"], l["taxe_code"], l["base_taxe"] is not None):
              (l["debit"], l["credit"]) for l in f["lignes"]}
    assert detail[("7011", "TVA18", False)] == (D(0), D(900))
    assert detail[("4431", "TVA18", True)] == (D(0), D(162))
    assert detail[("706", "TVA5", False)] == (D(0), D(500))
    assert detail[("4431", "TVA5", True)] == (D(0), D(25))
    assert detail[("7011", None, False)] == (D(0), D(200))
    assert r["lettrage"] is None


def test_vente_non_assujetti(engine):
    c = Comptabilite(engine, "PETIT", poste="P1")
    c.initialiser(exercice=2026, assujetti_tva=False)
    r = c.comptabiliser_vente(Vente("V", J(2026, 1, 3), [LigneMontant("5000")]))
    assert lignes(r["facture"]) == [("4111", D(5000), D(0)), ("7011", D(0), D(5000))]


def test_vente_controles(compta):
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_vente(Vente("V", J(2026, 1, 3), []))
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_vente(Vente("V", J(2026, 1, 3), [LigneMontant("0")]))
    with pytest.raises(EcritureInvalide, match="monnaie rendue"):
        compta.comptabiliser_vente(Vente("V", J(2026, 1, 3), [LigneMontant("100")],
                                         reglements=[Reglement("150")]))
    with pytest.raises(ParametreInvalide):
        compta.comptabiliser_vente(Vente("V", J(2026, 1, 3),
                                         [LigneMontant("100", taxe="TVA18-ACH")]))
    with pytest.raises(JournalInconnu):
        compta.comptabiliser_vente(Vente("V", J(2026, 1, 3), [LigneMontant("100")],
                                         reglements=[Reglement("100", compte="5215")]))
    # Rien n'a été écrit : tout ou rien.
    assert compta.ecritures(statut=None) == []


def test_vente_a_credit_reglements_partiels(compta):
    compta.comptabiliser_vente(Vente("V3", J(2026, 3, 1), [LigneMontant("1000")],
                                     tiers_ref="C7", tiers_nom="Awa",
                                     date_echeance=J(2026, 3, 31)))
    r1 = compta.comptabiliser_reglement_client("R1", "2026-03-10", "400",
                                               vente_source_id="V3", tiers_ref="C7")
    assert r1["lettrage"] is None
    r2 = compta.comptabiliser_reglement_client("R2", "2026-03-20", "600",
                                               vente_source_id="V3", tiers_ref="C7",
                                               compte="5211")
    assert r2["lettrage"] is not None
    assert r2["reglement"]["journal"] == "BQ"
    assert compta.lignes_non_lettrees("4111", tiers_ref="C7") == []
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_reglement_client("R3", "2026-03-20", "0")


def test_session_de_l_erp_tout_ou_rien(engine, compta):
    """La vente de l'ERP et son écriture sont dans LA MÊME transaction."""
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        compta.comptabiliser_vente(Vente("V4", J(2026, 3, 1), [LigneMontant("100")]),
                                   session=s)
        s.rollback()                      # l'ERP échoue après coup
    assert compta.ecritures() == []
    with Session(engine) as s:
        compta.comptabiliser_vente(Vente("V4", J(2026, 3, 1), [LigneMontant("100")]),
                                   session=s)
        s.commit()
    assert len(compta.ecritures()) == 1


# ---------------------------------------------------------------------------
# Achats
# ---------------------------------------------------------------------------

def test_achat_ht_et_reglement(compta):
    r = compta.comptabiliser_achat(Achat(
        "A1", J(2026, 1, 5), [LigneMontant("10000"),
                              LigneMontant("2000", compte="6015", taxe="TVA18-FG")],
        tiers_ref="F1", tiers_nom="Grossiste", reference="FA-77"))
    assert lignes(r["facture"]) == [
        ("6011", D(10000), D(0)), ("6015", D(2000), D(0)),
        ("4452", D(1800), D(0)), ("4454", D(360), D(0)),
        ("4011", D(0), D(14160))]
    p = compta.comptabiliser_reglement_fournisseur("P1", "2026-01-20", "14160",
                                                   achat_source_id="A1",
                                                   tiers_ref="F1", compte="5211")
    assert lignes(p["reglement"]) == [("5211", D(0), D(14160)),
                                      ("4011", D(14160), D(0))]
    assert p["lettrage"] is not None


def test_achat_comptant_ttc(compta):
    r = compta.comptabiliser_achat(Achat("A2", J(2026, 1, 5), [LigneMontant("1180")],
                                         prix_ttc=True,
                                         reglements=[Reglement("1180")]))
    assert r["facture"]["total"] == D(1180)
    assert r["lettrage"] is not None


# ---------------------------------------------------------------------------
# Trésorerie
# ---------------------------------------------------------------------------

def test_depense_recette_virement_ecart(compta):
    d = compta.comptabiliser_depense("D1", "2026-01-31", "150000",
                                     compte_charge="6222", libelle="Loyer janvier")
    assert lignes(d) == [("6222", D(150000), D(0)), ("5711", D(0), D(150000))]
    d2 = compta.comptabiliser_depense("D2", "2026-01-31", "11800",
                                      compte_charge="6052", libelle="EDM",
                                      taxe="TVA18-FG", compte_tresorerie="5211")
    assert lignes(d2) == [("6052", D(10000), D(0)), ("4454", D(1800), D(0)),
                          ("5211", D(0), D(11800))]
    rc = compta.comptabiliser_recette("R1", "2026-02-01", "50000",
                                      compte_produit="7073", libelle="Location")
    assert lignes(rc) == [("5711", D(50000), D(0)), ("7073", D(0), D(50000))]
    v = compta.comptabiliser_virement_interne("VI1", "2026-02-02", "100000",
                                              de="5711", vers="5211")
    assert lignes(v) == [("5211", D(100000), D(0)), ("5711", D(0), D(100000))]
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_virement_interne("VI2", "2026-02-02", "1",
                                              de="5711", vers="5711")
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_virement_interne("VI3", "2026-02-02", "1",
                                              de="5711", vers="4111")
    m = compta.comptabiliser_ecart_caisse("EC1", "2026-02-03", "-500")
    assert lignes(m) == [("658", D(500), D(0)), ("5711", D(0), D(500))]
    e = compta.comptabiliser_ecart_caisse("EC2", "2026-02-04", "200")
    assert lignes(e) == [("5711", D(200), D(0)), ("758", D(0), D(200))]
    assert compta.comptabiliser_ecart_caisse("EC3", "2026-02-04", "0") is None


def test_acompte_puis_imputation(compta):
    a = compta.comptabiliser_acompte_client("AC1", "2026-03-01", "300",
                                            tiers_ref="C1", tiers_nom="Awa")
    assert lignes(a) == [("5711", D(300), D(0)), ("4191", D(0), D(300))]
    compta.comptabiliser_vente(Vente("V9", J(2026, 3, 10), [LigneMontant("1000")],
                                     tiers_ref="C1"))
    i = compta.imputer_acompte_client("IMP1", "2026-03-10", "300", tiers_ref="C1",
                                      vente_source_id="V9")
    assert lignes(i["imputation"]) == [("4191", D(300), D(0)), ("4111", D(0), D(300))]
    r = compta.comptabiliser_reglement_client("R9", "2026-03-15", "700",
                                              vente_source_id="V9", tiers_ref="C1")
    assert r["lettrage"] is not None
    assert compta.lignes_non_lettrees("4111", tiers_ref="C1") == []


# ---------------------------------------------------------------------------
# Paie
# ---------------------------------------------------------------------------

def bulletin(**kw):
    base = dict(source_id="BP1", date=J(2026, 1, 31), salarie_ref="E1",
                salarie_nom="Ali Traoré", brut="250000",
                retenues=[Retenue("INPS part salariale", "9000", "431"),
                          Retenue("AMO part salariale", "7650", "4332"),
                          Retenue("ITS", "15000", "4472")],
                charges_patronales=[ChargePatronale("INPS part patronale", "40000"),
                                    ChargePatronale("AMO part patronale", "8750",
                                                    compte_organisme="4332")])
    base.update(kw)
    return BulletinPaie(**base)


def test_paie(compta):
    r = compta.comptabiliser_paie(bulletin())
    assert r["net_a_payer"] == D(218350)
    l = lignes(r["bulletin"])
    assert ("6611", D(250000), D(0)) in l
    assert ("422", D(0), D(218350)) in l
    assert ("4472", D(0), D(15000)) in l
    assert ("6641", D(40000), D(0)) in l and ("6641", D(8750), D(0)) in l
    assert r["bulletin"]["journal"] == "PA"
    p = compta.comptabiliser_paiement_salaire("PAY1", "2026-02-01", "218350",
                                              salarie_ref="E1",
                                              bulletin_source_id="BP1",
                                              compte="5211")
    assert p["lettrage"] is not None
    s = soldes(compta)
    assert s.get("422", D(0)) == D(0)
    assert s["431"] == D(-49000)


def test_paie_avec_avance(compta):
    compta.comptabiliser_avance_salarie("AV1", "2026-01-15", "50000", salarie_ref="E1")
    r = compta.comptabiliser_paie(bulletin(avances_deduites="50000"))
    assert r["net_a_payer"] == D(168350)
    assert compta.lignes_non_lettrees("421", tiers_ref="E1") == []


def test_paie_controles(compta):
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_paie(bulletin(brut="0"))
    with pytest.raises(EcritureInvalide, match="négatif"):
        compta.comptabiliser_paie(bulletin(avances_deduites="300000"))
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_paie(bulletin(retenues=[Retenue("x", "-1", "431")]))


# ---------------------------------------------------------------------------
# Stocks
# ---------------------------------------------------------------------------

def test_inventaire(compta):
    e = compta.comptabiliser_inventaire("INV", "2026-12-31", stock_initial="0",
                                        stock_final="750000")
    assert lignes(e) == [("311", D(750000), D(0)), ("6031", D(0), D(750000))]
    e2 = compta.comptabiliser_inventaire("INV2", "2026-12-31",
                                         stock_initial="750000", stock_final="500000")
    assert lignes(e2) == [("6031", D(750000), D(0)), ("311", D(0), D(750000)),
                          ("311", D(500000), D(0)), ("6031", D(0), D(500000))]
    assert compta.comptabiliser_inventaire("INV3", "2026-12-31", stock_initial=0,
                                           stock_final=0) is None
    with pytest.raises(EcritureInvalide):
        compta.comptabiliser_inventaire("INV4", "2026-12-31", stock_initial=-1,
                                        stock_final=0)
