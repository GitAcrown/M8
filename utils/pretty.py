"""Utilitaires de formatage de texte partagés entre les cogs."""


def shorten_text(text: str, max_length: int, *, end: str = "...") -> str:
    """Raccourcit le texte (si nécessaire) à la taille maximale indiquée.

    :param text: Texte à raccourcir
    :param max_length: Longueur maximale du texte
    :param end: Suffixe ajouté si le texte est raccourci
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(end)] + end
