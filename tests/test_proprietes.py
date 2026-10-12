"""Tests de propriété (hypothesis) : quelles que soient les opérations, les
invariants comptables tiennent."""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from compta_ohada import (Achat, Comptabilite, LigneMontant, Reglement, Vente,
                          creer_schema)
from compta_ohada.taxes import ventiler
from conftest import D, moteur_sqlite

montants = st.decimals(min_value=Decimal("1"), max_value=Decimal("5000000"),
                       places=2, allow_nan=False, allow_infinity=False)


@given(lignes=st.lists(montants, min_size=1, max_size=8),
       taux=st.sampled_from([D(0), D(5), D(18), D("19.25")]),
       ttc=st.booleans())
def test_ventilation_conserve_les_totaux(lignes, taux, ttc):
    v, t = ventiler([("7011", "T", m) for m in lignes], {"T": (taux, "4431")},
                    prix_ttc=ttc)
    total = sum(lignes, D(0))
    tva = t[0].tva
    if ttc:
        assert sum(x.ttc for x in v) == total
        assert sum(x.ht for x in v) + tva == total
    else:
        assert sum(x.ht for x in v) == total
        assert sum(x.ttc for x in v) == total + tva
    assert t[0].base == sum(x.ht for x in v)
    # La TVA ne s'écarte jamais de plus d'un centime du calcul théorique.
    theorique = (t[0].base * taux / 100)
    assert abs(tva - theorique) <= D("0.01")


operation = st.one_of(
    st.tuples(st.just("vente"), montants, st.booleans(), st.integers(0, 364)),
    st.tuples(st.just("achat"), montants, st.booleans(), st.integers(0, 364)),
    st.tuples(st.just("depense"), montants, st.booleans(), st.integers(0, 364)),
)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=st.lists(operation, min_size=1, max_size=12))
def test_invariants_des_etats(tmp_path_factory, ops):
    eng = moteur_sqlite(tmp_path_factory.mktemp("p") / "c.db")
    creer_schema(eng)
    c = Comptabilite(eng, "T", poste="P1")
    c.initialiser(exercice=2026)
    c.saisir("OD", date(2026, 1, 1), "Capital",
             [{"compte": "5211", "debit": "100000000"},
              {"compte": "101", "credit": "100000000"}])
    for i, (nature, m, paye, jour) in enumerate(ops):
        d = date(2026, 1, 1) + timedelta(days=jour)
        if nature == "vente":
            c.comptabiliser_vente(Vente(f"V{i}", d, [LigneMontant(m)],
                                        tiers_ref=f"C{i % 3}",
                                        reglements=[Reglement(m)] if paye else []))
        elif nature == "achat":
            c.comptabiliser_achat(Achat(f"A{i}", d, [LigneMontant(m)],
                                        tiers_ref=f"F{i % 2}",
                                        reglements=[Reglement(
                                            (m * Decimal("1.18")).quantize(D("0.01")),
                                            compte="5211")] if paye else []))
        else:
            c.comptabiliser_depense(f"D{i}", d, m, compte_charge="6222",
                                    libelle="Charge", compte_tresorerie="5211")
    balance = c.balance("2026-01-01", "2026-12-31")
    assert balance["equilibree"]
    bilan = c.bilan("2026")
    assert bilan["equilibre"], (bilan["total_actif"], bilan["total_passif"])
    resultat = c.compte_de_resultat("2026")["resultat_net"]
    assert bilan["resultat"] == resultat
    assert c.tableau_flux_tresorerie("2026")["ecart_controle"] == D(0)
    assert c.verifier_integrite()["ok"]
    cl = c.cloturer_exercice("2026")
    assert cl["resultat"] == resultat
    b27 = c.bilan("2027")
    assert b27["equilibre"] and b27["total_actif"] == bilan["total_actif"]
    eng.dispose()
