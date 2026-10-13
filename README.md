# compta-ohada

Cœur comptable **SYSCOHADA révisé** pour les ERP de Beyrey Consulting
(StockManager, EcoleManager…). C'est un paquet Python autonome : chaque ERP
l'installe comme une dépendance et lui confie sa comptabilité générale.

Il est conçu pour le terrain : postes **hors-ligne d'abord** (SQLite) qui se
synchronisent vers un serveur **PostgreSQL**, plusieurs caisses par boutique,
montants exacts au centime et inaltérabilité vérifiable.

> ⚠️ **À faire valider par un expert-comptable** inscrit à l'Ordre avant toute
> commercialisation présentée comme « conforme SYSCOHADA » : plan comptable,
> tables de passage du bilan et du compte de résultat, taux de TVA. Ce sont des
> données, regroupées dans `plan_syscohada.py` et `etats_financiers.py`, donc
> faciles à relire et à corriger.

---

## Ce que fait le paquet

| Domaine | Contenu |
|---|---|
| **Plan comptable** | SYSCOHADA révisé, classes 1 à 9 (≈ 550 comptes), installé par tenant ; sous-comptes propres au tenant (ex. 5521 « Orange Money ») |
| **Grand livre** | Partie double stricte, **append-only** : une écriture validée ne se modifie ni ne se supprime, elle se **contre-passe** |
| **Journaux** | Ventes (VT), Achats (AC), Caisse (CA), Banque (BQ), Monnaie électronique (MM), Opérations diverses (OD), Paie (PA), À-nouveaux (AN), et les vôtres |
| **Brouillard** | Écriture modifiable tant qu'elle n'est pas validée |
| **Numérotation** | Sans trou par journal, exercice et **poste** : `VT-2026-CAISSE1-00042` |
| **Inaltérabilité** | Garde-fou ORM + **chaîne d'empreintes SHA-256** ; `verifier_integrite()` désigne toute falsification, même faite directement en SQL |
| **Événements métier** | Vente, achat, règlements, dépense, recette, virement interne, écart de caisse, acompte, paie, avance sur salaire, inventaire : écritures générées automatiquement, de façon **idempotente** |
| **TVA** | Taux par tenant (18 % par défaut), prix TTC ou HT, multi-taux, exonéré, non assujetti, déclaration, liquidation (4441 / 4449) avec report du crédit |
| **Lettrage** | Manuel, automatique par document (facture + règlements) ou par tiers ; balance âgée FIFO |
| **États** | Balance générale six colonnes, grand livre, journaux, centralisateur, balance des tiers, balance âgée |
| **États financiers** | Bilan (brut / amortissements / net), compte de résultat (soldes XA → XI), **tableau des flux de trésorerie**, avec colonne N-1 |
| **Clôture** | Détermination du résultat (131/139), à-nouveaux **par tiers**, ouverture de N+1, verrouillage, affectation du résultat |
| **Contrôles** | Verrouillage de période, exercice clos, `controles()` après synchronisation (doubles contre-passations, doublons de génération…) |
| **Devises** *(0.2)* | Factures et règlements en EUR, USD…, parité EUR/XOF installée d'office, écarts de change réalisés (656/756), réévaluation de clôture (478/479, provision 194), comptes de trésorerie tenus en devise (676/776), extourne à l'ouverture |
| **Immobilisations** *(0.2)* | Fiches, plans linéaire (prorata 30/360) et dégressif, dotations automatiques idempotentes (mensuelles ou annuelles), cession, mise au rebut, reprise d'un parc existant, tableau des immobilisations et amortissements |
| **SMT** *(0.2)* | Système minimal de trésorerie : seuils de l'art. 13 AUDCIF, bilan et compte de résultat SMT, livre des recettes et dépenses |
| **Analytique** *(0.2)* | Sections par axe (rayon, magasin, classe, service), résultat analytique qui retombe toujours sur le résultat comptable |
| **Exports** *(0.2)* | Tous les états en Excel (montants numériques) et en PDF (en-tête de l'entreprise, pagination) |

Le SYSCOHADA révisé a **remplacé le TAFIRE** par le tableau des flux de
trésorerie (TFT) : c'est le TFT qui est produit.

## Installation

```bash
pip install "compta-ohada @ git+https://github.com/beyreyconsulting/compta-ohada@main"
# en production, épinglez une version (…/compta-ohada@v0.1.0) une fois le tag créé
# extras : [postgres] pour psycopg2, [migrations] pour Alembic
```

Python ≥ 3.11, SQLAlchemy 2.0.

## Démarrage rapide

```python
from datetime import date
from sqlalchemy import create_engine
from compta_ohada import (Comptabilite, creer_schema, Vente, Achat, LigneMontant,
                          Reglement, BulletinPaie, Retenue, ChargePatronale)

engine = create_engine("sqlite:///boutique.db")
creer_schema(engine)                       # ou migrations Alembic (voir plus bas)

compta = Comptabilite(engine, tenant_id="boutique-uuid", poste="CAISSE1",
                      utilisateur="awa")
compta.initialiser(exercice=2026)          # idempotent : plan, journaux, TVA 18 %

# Vente au comptant, prix TTC (usage du commerce de détail)
compta.comptabiliser_vente(Vente(
    source_id="vente-uuid", date=date(2026, 3, 2), reference="T-000123",
    lignes=[LigneMontant("11800")],
    reglements=[Reglement("11800", compte="5711")]))
# => VT : D 4111 11 800 / C 7011 10 000 / C 4431 1 800
#    CA : D 5711 11 800 / C 4111 11 800   (lettrées ensemble)

# Achat à crédit, prix HT, réglé plus tard par banque
compta.comptabiliser_achat(Achat("achat-uuid", date(2026, 3, 1),
                                 [LigneMontant("100000")], tiers_ref="F12",
                                 tiers_nom="Grossiste Sikasso"))
compta.comptabiliser_reglement_fournisseur("paiement-uuid", "2026-03-20",
                                           "118000", achat_source_id="achat-uuid",
                                           tiers_ref="F12", compte="5211")

# États
compta.balance("2026-01-01", "2026-12-31")
compta.bilan("2026")
compta.compte_de_resultat("2026")
compta.tableau_flux_tresorerie("2026")
compta.declaration_tva("2026-03-01", "2026-03-31")
compta.verifier_integrite()

# Fin d'exercice
compta.cloturer_exercice("2026")
compta.affecter_resultat("2026", "2027-06-30", reserve_legale="50000")
```

Toutes les méthodes renvoient des dictionnaires prêts à afficher. Les erreurs
dérivent de `ErreurComptable` (elle-même `ValueError`) et portent un message
rédigé pour être montré tel quel au gérant.

## Intégration dans un ERP (exemple : StockManager)

**1. Schéma.** Ajouter la branche Alembic du paquet à celle de l'ERP :

```python
# alembic/env.py de l'ERP
import compta_ohada
target_metadata = [models.Base.metadata, compta_ohada.metadata]
```
```ini
# alembic.ini
version_locations = %(here)s/alembic/versions <chemin de compta_ohada.migrations.DOSSIER_VERSIONS>
```
puis `alembic upgrade heads`.

**2. Synchronisation.** Fusionner le registre et le validateur :

```python
# sync_commun.py
from compta_ohada.synchro import TABLES_SYNC as TABLES_COMPTA, valider_ligne_entrante
TABLES_SYNC.update(TABLES_COMPTA)
# dans valider_ligne(modele, valeurs) :
if modele in TABLES_COMPTA.values():
    valider_ligne_entrante(modele, valeurs)
```

**3. Comptabiliser dans la MÊME transaction que l'opération métier** : passer
la session de l'ERP. La vente et son écriture sont enregistrées ensemble ou
pas du tout.

```python
# stock.enregistrer_vente
def _t(s):
    vente = Vente(...); s.add(vente); ...
    compta.comptabiliser_vente(compta_ohada.Vente(
        source_id=vente.id, date=vente.date_vente, reference=vente.numero_ticket,
        tiers_ref=vente.client_id, lignes=[LigneMontant(vente.montant_total)],
        reglements=[Reglement(vente.montant_paye, compte="5711")]
                   if vente.montant_paye else []), session=s)
executer_ecriture(engine, _t)

# stock.annuler_vente
compta.annuler_source("vente", vente_id, session=s)
```

**4. Un code poste unique par installation** (ex. le nom de la caisse) : il
préfixe les numéros d'écriture et porte sa propre chaîne d'empreintes.

## Principes de conception

- **Montants exacts.** Centimes en `BigInteger`, `Decimal` côté Python ; un
  `float` est refusé à l'entrée.
- **Append-only.** Comme `MouvementStock`, le grand livre ne se réécrit pas :
  un solde est toujours *dérivé* des lignes, jamais stocké. Deux postes
  hors-ligne ne peuvent donc pas s'écraser un solde.
- **Écriture scellée.** Une écriture validée est scellée par son total : la
  synchronisation peut recevoir ses lignes après l'en-tête, mais rien ne peut
  être ajouté au-delà.
- **Identifiants déterministes** (UUID v5) pour les référentiels : deux postes
  qui initialisent le même tenant hors-ligne créent les mêmes lignes, qui
  fusionnent à la synchronisation.
- **Idempotence.** Chaque génération automatique est clé par
  `(type de source, identifiant ERP)` : rejouer un appel renvoie l'écriture
  existante.
- **Verrou de numérotation** (`FOR NO KEY UPDATE` sur PostgreSQL) : pas de
  trou ni de doublon de numéro, même avec des validations simultanées.
- **Exercices.** Les états d'un exercice partent de ses à-nouveaux ; si
  l'exercice précédent n'est pas encore clôturé, l'ouverture est reconstituée
  « à blanc » (résultat en 130) et les états restent équilibrés.

## Paramètres du tenant

`compta.definir_parametre(cle, valeur)` — comptes par défaut (`compte_client`,
`compte_fournisseur`, `compte_vente_marchandises`, `compte_salaires`…),
journaux, taxe par défaut, devise. Les taux de cotisations sociales (INPS,
AMO) et le barème de l'ITS ne sont **pas** codés en dur : l'ERP calcule le
bulletin et transmet les montants (`BulletinPaie`).

## Tests

```bash
pip install -e ".[dev]"
pytest                                             # SQLite
COMPTA_TEST_PG_DSN=postgresql+psycopg2://user@hote/base pytest   # + PostgreSQL
```

La suite couvre la saisie, l'inaltérabilité (falsification SQL détectée), la
contre-passation, le lettrage, le verrouillage, chaque événement métier, les
états, la clôture, la synchronisation entre deux caisses hors-ligne, la
concurrence, la conformité de la migration Alembic, et des tests de propriété
(Hypothesis) qui vérifient sur des séquences aléatoires que la balance, le
bilan et le TFT restent équilibrés.

## Devises, immobilisations, SMT, analytique, exports (0.2)

```python
# Devises : la parité EUR (655,957) est installée ; les autres cours se saisissent.
compta.definir_cours("USD", "2026-03-01", "600")
compta.comptabiliser_vente(Vente("V9", date(2026, 3, 2), [LigneMontant("1000", taxe="")],
                                 tiers_ref="C-US", devise="USD"))
compta.comptabiliser_reglement_client("R9", "2026-04-02", "1000", vente_source_id="V9",
                                      tiers_ref="C-US", compte="5211", devise="USD")
compta.reevaluer_devises("2026-12-31")       # automatique à la clôture sinon

# Immobilisations
compta.creer_immobilisation("INFO-001", "Ordinateur", "2442", valeur_origine="900000",
                            date_acquisition="2026-06-01", duree_mois=36,
                            contrepartie="481", tiers_ref="F2")
compta.comptabiliser_dotations("2026-12-31")
compta.ceder_immobilisation("INFO-001", "2027-09-30", prix="300000", compte_tresorerie="5711")

# Très petites entités
compta.etats_smt("2026", activite="negoce")
compta.livre_recettes_depenses("2026-01-01", "2026-12-31")

# Analytique
compta.creer_section("QUINC", "Quincaillerie", axe="RAYON")
compta.resultat_analytique("2026-01-01", "2026-12-31", axe="RAYON")

# Exports (pip install compta-ohada[exports])
compta.definir_parametre("raison_sociale", "Quincaillerie Alpha SARL")
compta.exporter_etat("bilan", "bilan_2026.pdf", exercice="2026")
compta.exporter_etat("balance", "balance_2026.xlsx", debut="2026-01-01", fin="2026-12-31")
```

**Mise à jour depuis 0.1.0** : `alembic upgrade heads` (migration `compta_0002`,
colonnes nouvelles nullables), puis `initialiser()` sur chaque tenant
(idempotent) pour installer la parité EUR et les nouveaux paramètres. Les
empreintes des écritures 0.1.0 restent valides.

## Points à faire valider par l'expert-comptable

- Plan comptable et tables de passage (bilan, compte de résultat, TFT, SMT).
- Taux de TVA (18 % par défaut) et barème des coefficients dégressifs (1,5 /
  2 / 2,5 selon la durée) — propres à chaque Code général des impôts.
- Convention de prorata linéaire (jours, base 360).
- Traitement des écarts de conversion (478/479, provision 194, extourne).

## Limites connues (v0.2)

- Les notes annexes de la liasse ne sont pas produites (le tableau des
  immobilisations en fournit une partie).
- Pas de répartition analytique automatique (clés de répartition) : une ligne
  porte une seule section.
- Règlement partiel en devise d'un tiers ayant plusieurs factures ouvertes :
  le cours historique utilisé est le cours moyen pondéré de ses factures.

## Licence

Logiciel propriétaire — © Matec Pro SARL (Beyrey Consulting). Voir `LICENSE`.
