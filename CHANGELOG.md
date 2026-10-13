# Journal des versions

## 0.2.0 — 2026-10-13

- **Devises** : factures et règlements en devise, cours par date (parité EUR
  installée d'office), écarts de change réalisés 656/756, réévaluation des
  créances, dettes et trésoreries en devise (478/479, 676/776), provision pour
  perte de change (194, 697/797), réévaluation automatique à la clôture,
  à-nouveaux par tiers ET par devise, extourne des écarts de conversion.
- **Immobilisations** : fiches, plans linéaire et dégressif, dotations
  automatiques idempotentes, cession, mise au rebut, reprise d'existant,
  tableau des immobilisations, alerte de dotations oubliées à la clôture.
- **Système minimal de trésorerie** : seuils art. 13 AUDCIF, bilan et compte
  de résultat SMT, livre des recettes et dépenses.
- **Analytique** : sections par axe, résultat analytique.
- **Exports** Excel et PDF de tous les états.
- Règlement d'une facture en devise refusé s'il est saisi dans une autre
  devise (évite un écart de change fictif à la clôture).
- Migration Alembic `compta_0002` ; empreintes 0.1.0 inchangées.

## 0.1.0 — 2026-10-12

Première version : plan SYSCOHADA révisé, grand livre en partie double
append-only, journaux, brouillard, numérotation par poste, chaîne d'empreintes,
contre-passation, lettrage, verrouillage, événements métier (vente, achat,
règlements, dépense, recette, virement, écart de caisse, acompte, paie,
inventaire), TVA, états (balance, grand livre, journaux, tiers, balance âgée),
bilan, compte de résultat, tableau des flux de trésorerie, clôture et
affectation du résultat, intégration synchronisation, migration Alembic.
