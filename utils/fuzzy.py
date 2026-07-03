"""Utilitaires de recherche approximative (fuzzy matching), utilisés notamment pour l'autocomplétion."""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")

_word_regex = re.compile(r"\W", re.IGNORECASE)


def finder(
    text: str,
    collection: Iterable[T],
    *,
    key: Optional[Callable[[T], str]] = None,
) -> list[T]:
    """Filtre et trie une collection en fonction de la proximité de chaque élément avec `text`."""
    suggestions: list[tuple[int, int, T]] = []
    text = str(text)
    pattern = ".*?".join(map(re.escape, text))
    regex = re.compile(pattern, flags=re.IGNORECASE)
    for item in collection:
        to_search = key(item) if key else str(item)
        match = regex.search(to_search)
        if match:
            suggestions.append((len(match.group()), match.start(), item))

    def sort_key(tup: tuple[int, int, T]):
        return (tup[0], tup[1], key(tup[2]) if key else tup[2])

    return [item for *_, item in sorted(suggestions, key=sort_key)]


def find(text: str, collection: Iterable[str], *, key: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """Renvoie le meilleur résultat de `finder`, ou `None` si aucun résultat."""
    results = finder(text, collection, key=key)
    return results[0] if results else None
