"""Plan comptable, initialisation, référentiels (comptes, journaux, taxes, exercices)."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from compta_ohada import (Comptabilite, Compte, CompteInconnu, CompteNonImputable,
                          ExerciceIntrouvable, JournalInconnu, Ligne,
                          ParametreInvalide, TaxeInconnue)
from compta_ohada.modeles import id_deterministe
from compta_ohada.plan_syscohada import (PLAN_SYSCOHADA, classe_de,
                                         est_lettrable_par_defaut,
                                         libelle_standard)
from conftest import D, J, moteur_sqlite


def test_plan_couvre_les_neuf_classes():
    classes = {n for n, _ in PLAN_SYSCOHADA if len(n) == 1}
    assert classes == {str(i) for i in range(1, 10)}
    numeros = [n for n, _ in PLAN_SYSCOHADA]
    assert len(numeros) == len(set(numeros))
    for indispensable in ("101", "121", "131", "139", "2442", "311", "4011",
                          "4111", "4191", "421", "422", "431", "4431", "4441",
                          "4449", "4452", "4454", "4472", "481", "485", "5211",
                          "552", "5711", "6011", "6031", "6222", "6611", "6641",
                          "681", "7011", "706", "758", "658", "891"):
        assert libelle_standard(indispensable), indispensable


def test_chaque_compte_a_sa_rubrique():
    numeros = {n for n, _ in PLAN_SYSCOHADA}
    for n in numeros:
        if len(n) >= 2:
            assert n[:2] in numeros, n
        assert classe_de(n) == int(n[0])


def test_lettrables_par_defaut():
    assert est_lettrable_par_defaut("4111")
    assert est_lettrable_par_defaut("401")
    assert est_lettrable_par_defaut("422")
    assert not est_lettrable_par_defaut("41")       # rubrique
    assert not est_lettrable_par_defaut("4431")     # État : pas de lettrage
    assert not est_lettrable_par_defaut("5711")


def test_initialiser_est_idempotent(compta):
    second = compta.initialiser(exercice=2026)
    assert second == {"comptes": 0, "journaux": 0, "taxes": 0, "parametres": 0}
    assert {j["code"] for j in compta.journaux()} == {
        "VT", "AC", "CA", "BQ", "MM", "OD", "PA", "AN"}
    assert {t["code"] for t in compta.taxes()} >= {"TVA18", "TVA18-ACH", "EXO"}
    assert compta.parametre("devise") == "XOF"
    assert [e["code"] for e in compta.exercices()] == ["2026"]


def test_initialiser_conserve_les_personnalisations(compta):
    compta.modifier_compte("5711", libelle="Caisse boutique Badalabougou")
    compta.initialiser(exercice=2026)
    assert [c for c in compta.comptes(prefixe="5711")][0]["libelle"] == \
        "Caisse boutique Badalabougou"


def test_identifiants_deterministes_entre_postes(tmp_path):
    """Deux postes qui initialisent le même tenant hors-ligne produisent les
    mêmes UUID : à la synchronisation, les référentiels fusionnent."""
    ids = []
    for nom in ("poste_a.db", "poste_b.db"):
        eng = moteur_sqlite(tmp_path / nom)
        from compta_ohada import creer_schema
        creer_schema(eng)
        Comptabilite(eng, "T", poste=nom[:7].upper().replace(".", "")).initialiser(
            exercice=2026)
        with Session(eng) as s:
            ids.append(sorted(s.scalars(select(Compte.id)).all()))
    assert ids[0] == ids[1]
    assert id_deterministe("T", "compte", "5711") in ids[0]


def test_tva_non_assujetti(engine):
    c = Comptabilite(engine, "PETIT", poste="P1")
    c.initialiser(exercice=2026, assujetti_tva=False)
    assert c.parametre("assujetti_tva") == "0"
    assert c.parametre("taxe_vente_defaut") == "EXO"


def test_taux_tva_personnalise(engine):
    c = Comptabilite(engine, "X", poste="P1")
    c.initialiser(exercice=2026, taux_tva="19.25")
    codes = {t["code"]: t for t in c.taxes()}
    assert codes["TVA19.25"]["taux"] == D("19.25")


@pytest.mark.parametrize("taux", ["-1", "100", "abc"])
def test_taux_tva_invalide(engine, taux):
    with pytest.raises(ValueError):
        Comptabilite(engine, "X", poste="P1").initialiser(taux_tva=taux)


def test_creer_compte(compta):
    c = compta.creer_compte("5521", "Orange Money")
    assert c["classe"] == 5 and c["imputable"] and not c["lettrable"]
    tiers = compta.creer_compte("41101", "Clients grossistes")
    assert tiers["lettrable"]
    with pytest.raises(CompteInconnu):
        compta.creer_compte("5521", "Doublon")
    with pytest.raises(CompteNonImputable):
        compta.creer_compte("57", "Rubrique")
    with pytest.raises(CompteInconnu):
        compta.creer_compte("0123", "Zéro")
    with pytest.raises(CompteInconnu):
        compta.creer_compte("57a1", "Lettres")


def test_creer_compte_exige_un_tenant_initialise(engine):
    c = Comptabilite(engine, "VIDE", poste="P1")
    with pytest.raises(CompteInconnu):
        c.creer_compte("5521", "Orange Money")


def test_desactiver_compte(compta):
    compta.creer_compte("5718", "Caisse 2")
    compta.desactiver_compte("5718")
    assert not compta.comptes(prefixe="5718")
    with pytest.raises(CompteInconnu):
        compta.saisir("OD", J(2026, 1, 5), "x",
                      [Ligne("5718", debit=1), Ligne("101", credit=1)])
    # Réactivation par re-création.
    compta.creer_compte("5718", "Caisse 2 rouverte")
    assert compta.comptes(prefixe="5718")[0]["libelle"] == "Caisse 2 rouverte"


def test_desactiver_compte_avec_solde_refuse(compta):
    compta.saisir("OD", J(2026, 1, 5), "Apport",
                  [Ligne("5711", debit=1000), Ligne("101", credit=1000)])
    with pytest.raises(CompteInconnu):
        compta.desactiver_compte("5711")


def test_journaux(compta):
    compta.creer_compte("5521", "Orange Money")
    j = compta.creer_journal("OM", "Orange Money", "TRESORERIE",
                             compte_tresorerie="5521")
    assert j["compte_tresorerie"] == "5521"
    assert compta.journal_tresorerie_pour("5521") == "OM"
    with pytest.raises(JournalInconnu):
        compta.creer_journal("OM", "Doublon", "TRESORERIE", compte_tresorerie="5521")
    with pytest.raises(JournalInconnu):
        compta.creer_journal("X1", "Sans compte", "TRESORERIE")
    with pytest.raises(JournalInconnu):
        compta.creer_journal("X2", "Mauvais compte", "TRESORERIE",
                             compte_tresorerie="4111")
    with pytest.raises(JournalInconnu):
        compta.creer_journal("X3", "Type", "INCONNU")
    with pytest.raises(JournalInconnu):
        compta.journal_tresorerie_pour("5215")


def test_taxes(compta):
    t = compta.creer_taxe("TVA5", "TVA réduite 5 %", "5", "VENTE", "4431")
    assert t["taux"] == D(5)
    with pytest.raises(TaxeInconnue):
        compta.creer_taxe("TVA5", "x", "5", "VENTE", "4431")
    with pytest.raises(TaxeInconnue):
        compta.creer_taxe("TX", "x", "5", "VENTE", "7011")     # pas un 44x
    with pytest.raises(TaxeInconnue):
        compta.creer_taxe("TY", "x", "5", "AUTRE", "4431")
    with pytest.raises(TaxeInconnue):
        compta.creer_taxe("TZ", "x", "150", "VENTE", "4431")


def test_exercices(compta):
    compta.ouvrir_exercice("2027", "2027-01-01", "2027-12-31")
    with pytest.raises(ExerciceIntrouvable):
        compta.ouvrir_exercice("2027b", "2027-06-01", "2028-05-31")   # chevauche
    with pytest.raises(ExerciceIntrouvable):
        compta.ouvrir_exercice("X", "2030-01-01", "2029-12-31")        # inversé
    with pytest.raises(ExerciceIntrouvable):
        compta.ouvrir_exercice("Y", "2030-01-01", "2032-06-30")        # > 24 mois
    with pytest.raises(ExerciceIntrouvable):
        compta.saisir("OD", J(2031, 1, 1), "hors exercice",
                      [Ligne("5711", debit=1), Ligne("101", credit=1)])


def test_parametres(compta):
    compta.definir_parametre("compte_client", "4112")
    assert compta.parametre("compte_client") == "4112"
    with pytest.raises(CompteInconnu):
        compta.definir_parametre("compte_client", "4999")
    with pytest.raises(ParametreInvalide):
        compta.definir_parametre("date_verrouillage", "2026-01-01")


@pytest.mark.parametrize("tenant,poste", [("", "P1"), ("x" * 40, "P1"),
                                          ("T", ""), ("T", "P 1"), ("T", "P" * 21)])
def test_construction_invalide(engine, tenant, poste):
    with pytest.raises(ParametreInvalide):
        Comptabilite(engine, tenant, poste=poste)


def test_isolation_des_tenants(engine):
    a = Comptabilite(engine, "A", poste="P1")
    b = Comptabilite(engine, "B", poste="P1")
    a.initialiser(exercice=2026)
    b.initialiser(exercice=2026)
    a.saisir("OD", J(2026, 2, 1), "Apport A",
             [Ligne("5711", debit=500), Ligne("101", credit=500)])
    assert b.ecritures() == []
    assert b.balance("2026-01-01", "2026-12-31")["lignes"] == []
    assert len(a.ecritures()) == 1
    with pytest.raises(Exception):
        b.contrepasser(a.ecritures()[0]["id"], date_ecriture="2026-02-02")
