"""Contre-passation, lettrage, verrouillage de période, contrôles."""

import pytest

from compta_ohada import (DejaContrepassee, EcritureInvalide, ExerciceClos,
                          LettrageInvalide, Ligne, LigneMontant, PeriodeVerrouillee,
                          Reglement, Vente)
from conftest import D, J


def facture_client(compta, montant=1000, tiers="C1", jour=J(2026, 3, 1)):
    return compta.saisir("VT", jour, "Facture", [
        Ligne("4111", debit=montant, tiers_ref=tiers),
        Ligne("7011", credit=montant)])


def reglement(compta, montant, tiers="C1", jour=J(2026, 3, 5)):
    return compta.saisir("CA", jour, "Règlement", [
        Ligne("5711", debit=montant), Ligne("4111", credit=montant, tiers_ref=tiers)])


# ---------------------------------------------------------------------------
# Contre-passation
# ---------------------------------------------------------------------------

def test_contrepassation(compta):
    e = facture_client(compta)
    c = compta.contrepasser(e["id"], date_ecriture="2026-03-02", motif="erreur")
    assert c["type"] == "CONTREPASSATION" and c["contrepasse_id"] == e["id"]
    assert c["libelle"].startswith(f"Contre-passation {e['numero']}")
    assert [(l["compte"], l["debit"], l["credit"]) for l in c["lignes"]] == [
        ("4111", D(0), D(1000)), ("7011", D(1000), D(0))]
    soldes = {l["compte"]: l for l in compta.balance("2026-01-01", "2026-12-31")["lignes"]}
    assert soldes["4111"]["solde_debit"] == soldes["4111"]["solde_credit"] == D(0)
    # L'original et son inverse sont lettrés : la créance a disparu.
    assert compta.lignes_non_lettrees("4111", tiers_ref="C1") == []
    with pytest.raises(DejaContrepassee):
        compta.contrepasser(e["id"], date_ecriture="2026-03-03")
    assert compta.verifier_integrite()["ok"]


def test_contrepassation_controles(compta):
    b = facture_client(compta, 10)
    with pytest.raises(EcritureInvalide):
        compta.contrepasser(b["id"], date_ecriture="2026-02-01")   # avant l'original
    brouillard = compta.saisir("OD", J(2026, 3, 1), "b",
                               [Ligne("5711", debit=1), Ligne("101", credit=1)],
                               valider=False)
    with pytest.raises(EcritureInvalide):
        compta.contrepasser(brouillard["id"], date_ecriture="2026-03-02")
    with pytest.raises(EcritureInvalide):
        compta.contrepasser("inconnue", date_ecriture="2026-03-02")


def test_contrepassation_delettre_puis_relettre(compta):
    f = facture_client(compta, 500)
    r = reglement(compta, 500)
    code = compta.lettrer([f["lignes"][0]["id"], r["lignes"][1]["id"]])
    assert code.startswith("P1-")
    compta.contrepasser(f["id"], date_ecriture="2026-03-10")
    # Le règlement redevient disponible : c'est un avoir du client.
    ouvertes = compta.lignes_non_lettrees("4111", tiers_ref="C1")
    assert [(l["debit"], l["credit"]) for l in ouvertes] == [(D(0), D(500))]


def test_annuler_source_vente_et_reglements(compta):
    compta.comptabiliser_vente(Vente("V1", J(2026, 4, 1), [LigneMontant("1180")],
                                     reglements=[Reglement("1180")]))
    inverses = compta.annuler_source("vente", "V1", date_ecriture="2026-04-02")
    assert len(inverses) == 2
    assert compta.annuler_source("vente", "V1", date_ecriture="2026-04-02") == []
    bal = {l["compte"]: l for l in compta.balance("2026-01-01", "2026-12-31")["lignes"]}
    for compte in ("4111", "5711", "7011", "4431"):
        assert bal[compte]["solde_debit"] == bal[compte]["solde_credit"] == D(0)
    # Après annulation, la même vente peut être recomptabilisée (corrigée).
    v = compta.comptabiliser_vente(Vente("V1", J(2026, 4, 3), [LigneMontant("590")]))
    assert v["facture"]["total"] == D(590)
    assert compta.controles() == []


# ---------------------------------------------------------------------------
# Lettrage
# ---------------------------------------------------------------------------

def test_lettrage_regles(compta):
    f = facture_client(compta, 300)
    r1 = reglement(compta, 100)
    r2 = reglement(compta, 200)
    lf, l1, l2 = f["lignes"][0]["id"], r1["lignes"][1]["id"], r2["lignes"][1]["id"]
    with pytest.raises(LettrageInvalide, match="solde nul"):
        compta.lettrer([lf, l1])
    with pytest.raises(LettrageInvalide):
        compta.lettrer([lf])
    with pytest.raises(LettrageInvalide, match="même compte"):
        compta.lettrer([lf, r1["lignes"][0]["id"]])
    with pytest.raises(LettrageInvalide, match="lettrable"):
        compta.lettrer([r1["lignes"][0]["id"], r2["lignes"][0]["id"]])
    code = compta.lettrer([lf, l1, l2])
    with pytest.raises(LettrageInvalide, match="déjà lettrée"):
        compta.lettrer([lf, l1, l2])
    compta.delettrer(code)
    assert len(compta.lignes_non_lettrees("4111", tiers_ref="C1")) == 3
    with pytest.raises(LettrageInvalide):
        compta.delettrer("INCONNU")


def test_lettrage_tiers_differents_refuse(compta):
    f = facture_client(compta, 100, tiers="C1")
    r = reglement(compta, 100, tiers="C2")
    with pytest.raises(LettrageInvalide, match="même tiers"):
        compta.lettrer([f["lignes"][0]["id"], r["lignes"][1]["id"]])


def test_lettrage_automatique(compta):
    facture_client(compta, 300)
    reglement(compta, 100)
    assert compta.lettrage_automatique("4111", tiers_ref="C1") is None
    reglement(compta, 200)
    assert compta.lettrage_automatique("4111", tiers_ref="C1")
    assert compta.lignes_non_lettrees("4111", tiers_ref="C1") == []


# ---------------------------------------------------------------------------
# Verrouillage
# ---------------------------------------------------------------------------

def test_verrouillage_de_periode(compta):
    facture_client(compta, 10, jour=J(2026, 1, 15))
    compta.verrouiller_jusqu_au("2026-01-31")
    assert compta.date_verrouillage() == J(2026, 1, 31)
    with pytest.raises(PeriodeVerrouillee):
        facture_client(compta, 10, jour=J(2026, 1, 31))
    facture_client(compta, 10, jour=J(2026, 2, 1))
    with pytest.raises(PeriodeVerrouillee, match="forcer"):
        compta.verrouiller_jusqu_au("2026-01-15")
    compta.verrouiller_jusqu_au("2026-01-15", forcer=True)
    facture_client(compta, 10, jour=J(2026, 1, 20))


def test_verrouillage_refuse_avec_brouillards(compta):
    compta.saisir("OD", J(2026, 1, 10), "b",
                  [Ligne("5711", debit=1), Ligne("101", credit=1)], valider=False)
    with pytest.raises(PeriodeVerrouillee, match="brouillard"):
        compta.verrouiller_jusqu_au("2026-01-31")


def test_brouillard_ne_se_valide_pas_en_periode_verrouillee(compta):
    b = compta.saisir("OD", J(2026, 2, 10), "b",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)], valider=False)
    compta.verrouiller_jusqu_au("2026-01-31")
    with pytest.raises(PeriodeVerrouillee):
        compta.modifier_brouillard(b["id"], date_ecriture="2026-01-15")


def test_contrepassation_en_periode_verrouillee(compta):
    e = facture_client(compta, 10, jour=J(2026, 1, 15))
    compta.verrouiller_jusqu_au("2026-01-31")
    with pytest.raises(PeriodeVerrouillee):
        compta.contrepasser(e["id"], date_ecriture="2026-01-20")
    compta.contrepasser(e["id"], date_ecriture="2026-02-01")


def test_controles_signale_une_double_contrepassation(engine, compta):
    """Deux postes hors-ligne qui contre-passent la même écriture : la
    synchronisation accepte les deux (rien ne se perd), ``controles`` le
    signale pour que le comptable annule le doublon."""
    from sqlalchemy.orm import Session
    from compta_ohada.modeles import Ecriture
    e = compta.saisir("OD", J(2026, 1, 6), "y", [Ligne("5711", debit=2),
                                                 Ligne("101", credit=2)])
    compta.contrepasser(e["id"], date_ecriture="2026-01-07")
    with Session(engine) as s:   # copie reçue d'un autre poste (P3)
        inverse = s.query(Ecriture).filter_by(contrepasse_id=e["id"]).one()
        s.add(Ecriture(**{c.key: getattr(inverse, c.key)
                          for c in Ecriture.__table__.columns
                          if c.key not in ("id", "sequence", "numero", "poste")},
                       poste="P3", sequence=1, numero="OD-2026-P3-00001"))
        s.commit()
    constats = compta.controles()
    assert any(c["controle"] == "contre-passation multiple" for c in constats)


def test_exercice_clos_refuse_toute_saisie(compta):
    compta.cloturer_exercice("2026")
    with pytest.raises((ExerciceClos, PeriodeVerrouillee)):
        facture_client(compta, 10, jour=J(2026, 6, 1))
