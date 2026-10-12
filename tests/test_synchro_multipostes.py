"""Synchronisation hors-ligne multi-postes (même mécanique « last-write-wins »
que sync_commun.appliquer_ligne des ERP Beyrey)."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Date, DateTime, select
from sqlalchemy.orm import Session

from compta_ohada import (TABLES_SYNC, Comptabilite, ErreurComptable, Ligne,
                          LigneEcriture, LigneMontant, Reglement, Vente,
                          creer_schema, valider_ligne_entrante, Ecriture)
from conftest import D, J, moteur_sqlite


def _naif(v):
    return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v


def vers_dict(obj) -> dict:
    out = {}
    for col in obj.__table__.columns:
        v = getattr(obj, col.name)
        if isinstance(v, Decimal):
            v = str(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out[col.name] = v
    return out


def depuis_dict(modele, data) -> dict:
    vals = {}
    for col in modele.__table__.columns:
        if col.name not in data:
            continue
        brut = data[col.name]
        if brut is None:
            vals[col.name] = None
        elif isinstance(col.type, DateTime):
            vals[col.name] = _naif(datetime.fromisoformat(brut))
        elif isinstance(col.type, Date):
            vals[col.name] = date.fromisoformat(brut)
        else:
            vals[col.name] = brut
    return vals


def appliquer(session, modele, data) -> str:
    vals = depuis_dict(modele, data)
    valider_ligne_entrante(modele, vals)
    vals["is_synced"] = True
    existant = session.get(modele, data["id"],
                           execution_options={"inclure_supprimes": True})
    if existant is None:
        session.add(modele(**vals))
        return "insere"
    if _naif(existant.updated_at) >= _naif(vals["updated_at"]):
        return "conserve"
    for k, v in vals.items():
        setattr(existant, k, v)
    return "maj"


def pousser(source, cible, *, tout=False) -> int:
    """Pousse les lignes non synchronisées de ``source`` vers ``cible``."""
    n = 0
    with Session(source) as s, Session(cible) as c:
        for modele in TABLES_SYNC.values():
            req = select(modele).execution_options(inclure_supprimes=True)
            if not tout:
                req = req.where(modele.is_synced.is_(False))
            lignes = list(s.scalars(req))
            for obj in lignes:
                appliquer(c, modele, vers_dict(obj))
                n += 1
            c.flush()
        c.commit()
        for modele in TABLES_SYNC.values():
            for obj in s.scalars(select(modele).where(modele.is_synced.is_(False))
                                 .execution_options(inclure_supprimes=True)):
                obj.is_synced = True
        s.commit()
    return n


@pytest.fixture
def trois_bases(tmp_path):
    bases = {}
    for nom in ("caisse1", "caisse2", "cloud"):
        eng = moteur_sqlite(tmp_path / f"{nom}.db")
        creer_schema(eng)
        bases[nom] = eng
    return bases


def test_deux_caisses_hors_ligne_puis_synchronisation(trois_bases):
    c1 = Comptabilite(trois_bases["caisse1"], "BOUTIQUE", poste="CAISSE1")
    c2 = Comptabilite(trois_bases["caisse2"], "BOUTIQUE", poste="CAISSE2")
    # Les deux postes initialisent le tenant SANS se voir.
    c1.initialiser(exercice=2026)
    c2.initialiser(exercice=2026)
    for i in range(5):
        c1.comptabiliser_vente(Vente(f"V1-{i}", J(2026, 3, 1 + i),
                                     [LigneMontant("1180")],
                                     reglements=[Reglement("1180")]))
        c2.comptabiliser_vente(Vente(f"V2-{i}", J(2026, 3, 1 + i),
                                     [LigneMontant("2360")],
                                     reglements=[Reglement("2360", compte="552")]))
    c2.comptabiliser_vente(Vente("V2-credit", J(2026, 3, 9), [LigneMontant("590")],
                                 tiers_ref="C1"))

    assert pousser(trois_bases["caisse1"], trois_bases["cloud"]) > 0
    assert pousser(trois_bases["caisse2"], trois_bases["cloud"]) > 0

    cloud = Comptabilite(trois_bases["cloud"], "BOUTIQUE", poste="SERVEUR")
    integrite = cloud.verifier_integrite()
    assert integrite["ok"], integrite["anomalies"]
    assert integrite["chaines"] == 4              # (VT, CA) x 2 postes, MM pour P2
    assert cloud.controles() == []
    numeros = {e["numero"] for e in cloud.ecritures()}
    assert "VT-2026-CAISSE1-00001" in numeros and "VT-2026-CAISSE2-00001" in numeros
    b = cloud.balance("2026-01-01", "2026-12-31")
    assert b["equilibree"]
    soldes = {l["compte"]: l for l in b["lignes"]}
    assert soldes["5711"]["solde_debit"] == D(5900)
    assert soldes["552"]["solde_debit"] == D(11800)
    assert soldes["4111"]["solde_debit"] == D(590)

    # Redescente : chaque caisse converge vers la même comptabilité.
    pousser(trois_bases["cloud"], trois_bases["caisse1"], tout=True)
    assert c1.balance("2026-01-01", "2026-12-31")["totaux"] == b["totaux"]
    assert c1.verifier_integrite()["ok"]

    # Rejouer la synchronisation n'altère rien (garde-fou + réécriture identique).
    pousser(trois_bases["cloud"], trois_bases["caisse1"], tout=True)
    assert c1.verifier_integrite()["ok"]


def test_reglement_saisi_sur_un_autre_poste(trois_bases):
    """Vente à crédit sur la caisse 1, règlement encaissé sur la caisse 2."""
    c1 = Comptabilite(trois_bases["caisse1"], "B", poste="CAISSE1")
    c2 = Comptabilite(trois_bases["caisse2"], "B", poste="CAISSE2")
    c1.initialiser(exercice=2026)
    c1.comptabiliser_vente(Vente("V", J(2026, 4, 1), [LigneMontant("1000")],
                                 tiers_ref="C1"))
    pousser(trois_bases["caisse1"], trois_bases["cloud"])
    pousser(trois_bases["cloud"], trois_bases["caisse2"], tout=True)
    r = c2.comptabiliser_reglement_client("R", "2026-04-05", "1000",
                                          vente_source_id="V", tiers_ref="C1")
    assert r["lettrage"] is not None
    pousser(trois_bases["caisse2"], trois_bases["cloud"])
    cloud = Comptabilite(trois_bases["cloud"], "B", poste="SRV")
    assert cloud.lignes_non_lettrees("4111", tiers_ref="C1") == []
    assert cloud.verifier_integrite()["ok"]


def test_ligne_entrante_corrompue_refusee():
    with pytest.raises(ErreurComptable):
        valider_ligne_entrante(LigneEcriture, {"debit": "10", "credit": "10",
                                               "compte_numero": "5711"})
    with pytest.raises(ErreurComptable):
        valider_ligne_entrante(LigneEcriture, {"debit": "-1", "credit": "0",
                                               "compte_numero": "5711"})
    with pytest.raises(ErreurComptable):
        valider_ligne_entrante(LigneEcriture, {"debit": "1", "credit": "0",
                                               "compte_numero": None})
    with pytest.raises(ErreurComptable):
        valider_ligne_entrante(Ecriture, {"statut": "VALIDEE", "numero": "X",
                                          "sequence": 1, "empreinte": "abc",
                                          "empreinte_precedente": "0" * 64})
    with pytest.raises(ErreurComptable):
        valider_ligne_entrante(Ecriture, {"statut": "VALIDEE", "numero": None})
    valider_ligne_entrante(LigneEcriture, {"debit": "1", "credit": "0",
                                           "compte_numero": "5711"})


def test_modification_distante_d_une_ecriture_validee_refusee(trois_bases):
    c1 = Comptabilite(trois_bases["caisse1"], "B", poste="CAISSE1")
    c1.initialiser(exercice=2026)
    e = c1.saisir("OD", J(2026, 1, 3), "Apport", [Ligne("5711", debit=10),
                                                  Ligne("101", credit=10)])
    pousser(trois_bases["caisse1"], trois_bases["cloud"])
    with Session(trois_bases["cloud"]) as s:
        lg = s.get(LigneEcriture, e["lignes"][0]["id"])
        data = vers_dict(lg)
    data["debit"] = "99.00"
    data["updated_at"] = datetime(2030, 1, 1).isoformat()
    from compta_ohada import EcritureVerrouillee
    with Session(trois_bases["caisse1"]) as s:
        appliquer(s, LigneEcriture, data)
        with pytest.raises(EcritureVerrouillee):
            s.flush()
