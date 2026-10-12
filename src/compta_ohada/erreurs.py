"""
Exceptions du cœur comptable.

Toutes dérivent de ``ErreurComptable`` (elle-même ``ValueError``, pour que
les écrans existants des ERP, qui interceptent ``ValueError`` et affichent le
message au gérant, fonctionnent sans changement). Les messages sont rédigés
pour être montrés tels quels à l'utilisateur.
"""


class ErreurComptable(ValueError):
    """Racine de toutes les erreurs métier comptables."""


class EcritureDesequilibree(ErreurComptable):
    """Total des débits différent du total des crédits."""


class EcritureInvalide(ErreurComptable):
    """Écriture mal formée (ligne vide, montant négatif, débit ET crédit…)."""


class CompteInconnu(ErreurComptable):
    """Numéro de compte absent du plan du tenant (ou désactivé)."""


class CompteNonImputable(ErreurComptable):
    """Compte de regroupement (classe, rubrique) : on n'y passe pas d'écriture."""


class JournalInconnu(ErreurComptable):
    """Code journal absent ou désactivé."""


class PeriodeVerrouillee(ErreurComptable):
    """Date d'écriture antérieure ou égale à la date de verrouillage."""


class ExerciceIntrouvable(ErreurComptable):
    """Aucun exercice ne couvre la date de l'écriture."""


class ExerciceClos(ErreurComptable):
    """L'exercice couvrant la date est clôturé."""


class EcritureVerrouillee(ErreurComptable):
    """Tentative de modifier ou supprimer une écriture validée.

    Une écriture validée est définitive : on la corrige par contre-passation."""


class DejaContrepassee(ErreurComptable):
    """L'écriture a déjà été contre-passée."""


class LettrageInvalide(ErreurComptable):
    """Lignes non lettrables ensemble (compte/tiers différents, non soldées…)."""


class TaxeInconnue(ErreurComptable):
    """Code de taxe absent ou désactivé."""


class IntegriteCompromise(ErreurComptable):
    """La chaîne d'empreintes ne correspond plus aux écritures stockées."""


class ParametreInvalide(ErreurComptable):
    """Paramètre de tenant manquant ou incohérent."""
