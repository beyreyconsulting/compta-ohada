"""Saisie en partie double, brouillard, numérotation, inaltérabilité, empreintes."""

from decimal import Decimal

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from compta_ohada import (Comptabilite, CompteInconnu, CompteNonImputable,
                          Ecriture, EcritureDesequilibree, EcritureInvalide,
                          EcritureVerrouillee, JournalInconnu, Ligne,
                          LigneEcriture, TaxeInconnue)
from conftest import D, J


def apport(compta, montant=1000, jour=J(2026, 1, 10), **kw):
    return compta.saisir("OD", jour, "Apport", [Ligne("5711", debit=montant),
                                                Ligne("101", credit=montant)], **kw)


# ---------------------------------------------------------------------------
# Règles de saisie
# ---------------------------------------------------------------------------

def test_saisie_validee_numerotee(compta):
    e = apport(compta, "1500.50")
    assert e["statut"] == "VALIDEE"
    assert e["numero"] == "OD-2026-P1-00001"
    assert e["total"] == D("1500.50")
    assert len(e["empreinte"]) == 64
    assert [lg["compte"] for lg in e["lignes"]] == ["5711", "101"]
    assert apport(compta)["numero"] == "OD-2026-P1-00002"


def test_sequence_par_journal_et_par_poste(engine, compta):
    apport(compta)
    autre_poste = Comptabilite(engine, "BOUTIQUE-1", poste="P2")
    e = autre_poste.saisir("BQ", J(2026, 1, 11), "Versement",
                           [Ligne("5211", debit=10), Ligne("5711", credit=10)])
    assert e["numero"] == "BQ-2026-P2-00001"
    assert autre_poste.saisir("OD", J(2026, 1, 11), "x",
                              [Ligne("5711", debit=1), Ligne("101", credit=1)]
                              )["numero"] == "OD-2026-P2-00001"


def test_desequilibre(compta):
    with pytest.raises(EcritureDesequilibree, match="écart 0.01"):
        compta.saisir("OD", J(2026, 1, 1), "x",
                      [Ligne("5711", debit="100.01"), Ligne("101", credit="100")])


@pytest.mark.parametrize("lignes,erreur", [
    ([Ligne("5711", debit=10)], EcritureInvalide),
    ([Ligne("5711", debit=-10), Ligne("101", credit=-10)], EcritureInvalide),
    ([Ligne("5711", debit=10, credit=10), Ligne("101", credit=0)], EcritureInvalide),
    ([Ligne("5711"), Ligne("101")], EcritureInvalide),
    ([Ligne("57", debit=10), Ligne("101", credit=10)], CompteNonImputable),
    ([Ligne("5799", debit=10), Ligne("101", credit=10)], CompteInconnu),
    ([Ligne("abc", debit=10), Ligne("101", credit=10)], CompteInconnu),
    ([Ligne("5711", debit=10, taxe_code="NOPE"), Ligne("101", credit=10)], TaxeInconnue),
    ([Ligne("5711", debit=10, base_taxe=-1), Ligne("101", credit=10)], EcritureInvalide),
    ([Ligne("901", debit=10), Ligne("101", credit=10)], EcritureDesequilibree),
])
def test_lignes_invalides(compta, lignes, erreur):
    with pytest.raises(erreur):
        compta.saisir("OD", J(2026, 1, 1), "x", lignes)
    assert compta.ecritures(statut=None) == []


def test_flottant_refuse(compta):
    with pytest.raises(TypeError):
        compta.saisir("OD", J(2026, 1, 1), "x",
                      [Ligne("5711", debit=0.1), Ligne("101", credit=0.1)])


def test_classe_9_equilibree_entre_elle(compta):
    e = compta.saisir("OD", J(2026, 1, 1), "Caution bancaire obtenue",
                      [Ligne("902", debit=5000), Ligne("911", credit=5000)])
    assert e["statut"] == "VALIDEE"


def test_libelles_nettoyes_et_bornes(compta):
    e = compta.saisir("OD", J(2026, 1, 1), "  Apport\x00 du\tgérant\n ",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])
    assert e["libelle"] == "Apport du gérant"
    with pytest.raises(EcritureInvalide):
        compta.saisir("OD", J(2026, 1, 1), "x" * 256,
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])
    with pytest.raises(EcritureInvalide):
        compta.saisir("OD", J(2026, 1, 1), "   ",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])
    with pytest.raises(EcritureInvalide):
        compta.saisir("OD", "31/01/2026", "x",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])


def test_journal_inconnu_et_journal_an_reserve(compta):
    with pytest.raises(JournalInconnu):
        compta.saisir("ZZ", J(2026, 1, 1), "x",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])
    with pytest.raises(EcritureInvalide):
        compta.saisir("AN", J(2026, 1, 1), "x",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])


def test_lignes_en_dict(compta):
    e = compta.saisir("OD", J(2026, 1, 1), "x",
                      [{"compte": "5711", "debit": "7"}, {"compte": "101", "credit": 7}])
    assert e["total"] == D(7)


def test_idempotence_par_source(compta):
    a = apport(compta, source_type="test", source_id="42")
    b = apport(compta, source_type="test", source_id="42")
    assert a["id"] == b["id"]
    assert len(compta.ecritures()) == 1
    with pytest.raises(EcritureInvalide):
        apport(compta, source_type="test")


# ---------------------------------------------------------------------------
# Brouillard
# ---------------------------------------------------------------------------

def test_brouillard_modifier_valider_supprimer(compta):
    b = apport(compta, 5, valider=False)
    assert b["statut"] == "BROUILLARD" and b["numero"] is None
    m = compta.modifier_brouillard(
        b["id"], lignes=[Ligne("5711", debit=8), Ligne("101", credit=8)],
        libelle="Apport corrigé", date_ecriture="2026-01-15", reference="PJ-1")
    assert m["total"] == D(8) and len(m["lignes"]) == 2
    assert m["date"] == J(2026, 1, 15) and m["reference"] == "PJ-1"
    v = compta.valider(b["id"])
    assert v["numero"] == "OD-2026-P1-00001" and v["total"] == D(8)
    assert len(v["lignes"]) == 2
    assert compta.verifier_integrite()["ok"]
    with pytest.raises(EcritureVerrouillee):
        compta.modifier_brouillard(b["id"], libelle="trop tard")
    with pytest.raises(EcritureVerrouillee):
        compta.supprimer_brouillard(b["id"])

    autre = apport(compta, 3, valider=False)
    compta.supprimer_brouillard(autre["id"])
    assert all(e["id"] != autre["id"] for e in compta.ecritures(statut=None))


def test_brouillard_absent_des_etats(compta):
    apport(compta, 100, valider=False)
    assert compta.balance("2026-01-01", "2026-12-31")["lignes"] == []


def test_valider_deux_fois_est_sans_effet(compta):
    b = apport(compta, valider=False)
    v1 = compta.valider(b["id"])
    v2 = compta.valider(b["id"])
    assert v1["numero"] == v2["numero"] and v1["empreinte"] == v2["empreinte"]


# ---------------------------------------------------------------------------
# Inaltérabilité : garde-fou ORM
# ---------------------------------------------------------------------------

def test_modifier_une_ecriture_validee_est_refuse(engine, compta):
    e = apport(compta)
    with Session(engine) as s:
        ecr = s.get(Ecriture, e["id"])
        ecr.libelle = "Falsifié"
        with pytest.raises(EcritureVerrouillee):
            s.flush()


def test_modifier_une_ligne_validee_est_refuse(engine, compta):
    e = apport(compta)
    with Session(engine) as s:
        lg = s.get(LigneEcriture, e["lignes"][0]["id"])
        lg.debit = Decimal("999")
        with pytest.raises(EcritureVerrouillee):
            s.flush()


def test_supprimer_ou_tombstoner_une_validee_est_refuse(engine, compta):
    e = apport(compta)
    with Session(engine) as s:
        s.delete(s.get(LigneEcriture, e["lignes"][0]["id"]))
        with pytest.raises(EcritureVerrouillee):
            s.flush()
    with Session(engine) as s:
        from compta_ohada.socle import marquer_supprime
        marquer_supprime(s.get(Ecriture, e["id"]))
        with pytest.raises(EcritureVerrouillee):
            s.flush()


def test_ajouter_une_ligne_a_une_validee_est_refuse(engine, compta):
    e = apport(compta)
    with Session(engine) as s:
        lg = s.get(LigneEcriture, e["lignes"][0]["id"])
        s.add(LigneEcriture(tenant_id=lg.tenant_id, ecriture_id=e["id"], ordre=3,
                            compte_id=lg.compte_id, compte_numero="5711",
                            libelle="pirate", debit=Decimal(1), credit=Decimal(0)))
        with pytest.raises(EcritureVerrouillee):
            s.flush()


def test_reecriture_identique_toleree(engine, compta):
    """La synchro « last-write-wins » réaffecte toutes les colonnes : si les
    valeurs sont identiques (même sous forme de chaînes), c'est accepté."""
    e = apport(compta, "1500.50")
    with Session(engine) as s:
        lg = s.get(LigneEcriture, e["lignes"][0]["id"])
        lg.debit = "1500.50"
        lg.is_synced = True
        ecr = s.get(Ecriture, e["id"])
        ecr.date_ecriture = J(2026, 1, 10)
        ecr.is_synced = True
        s.commit()


# ---------------------------------------------------------------------------
# Chaîne d'empreintes
# ---------------------------------------------------------------------------

def test_integrite_detecte_une_falsification_sql(engine, compta):
    for i in range(3):
        apport(compta, 100 + i)
    assert compta.verifier_integrite() == {"ok": True, "ecritures": 3,
                                           "chaines": 1, "anomalies": []}
    with engine.begin() as cx:   # attaque directe, hors ORM
        cx.execute(text("UPDATE compta_lignes SET debit = 99900 "
                        "WHERE compte_numero = '5711' AND debit = 10100"))
        cx.execute(text("UPDATE compta_lignes SET credit = 99900 "
                        "WHERE compte_numero = '101' AND credit = 10100"))
    rapport = compta.verifier_integrite()
    assert not rapport["ok"]
    assert any(a["anomalie"] == "contenu modifié après validation"
               for a in rapport["anomalies"])
    assert any(a["anomalie"].startswith("écriture déséquilibrée")
               for a in rapport["anomalies"])


def test_integrite_detecte_une_ecriture_supprimee(engine, compta):
    ids = [apport(compta, 10 + i)["id"] for i in range(3)]
    with engine.begin() as cx:
        cx.execute(text("DELETE FROM compta_lignes WHERE ecriture_id = :i"),
                   {"i": ids[1]})
        cx.execute(text("DELETE FROM compta_ecritures WHERE id = :i"), {"i": ids[1]})
    anomalies = compta.verifier_integrite()["anomalies"]
    assert {a["anomalie"] for a in anomalies} >= {"chaînage rompu"}
    assert any("séquence" in a["anomalie"] for a in anomalies)


def test_empreinte_chainee(compta):
    a = apport(compta)
    b = apport(compta)
    with Session(compta.engine) as s:
        eb = s.get(Ecriture, b["id"])
        assert eb.empreinte_precedente == a["empreinte"]
        ea = s.get(Ecriture, a["id"])
        assert ea.empreinte_precedente == "0" * 64


def test_ligne_dict_champ_inconnu(compta):
    with pytest.raises(EcritureInvalide, match="inconnu"):
        compta.saisir("OD", J(2026, 1, 1), "x",
                      [{"compte": "5711", "debitt": 1}, {"compte": "101", "credit": 1}])


def test_parametres_journal_et_taxe_verifies(compta):
    from compta_ohada import JournalInconnu, TaxeInconnue
    with pytest.raises(JournalInconnu):
        compta.definir_parametre("journal_ventes", "ZZ")
    with pytest.raises(TaxeInconnue):
        compta.definir_parametre("taxe_vente_defaut", "TVA99")
    with pytest.raises(CompteNonImputable):
        compta.definir_parametre("compte_client", "41")
