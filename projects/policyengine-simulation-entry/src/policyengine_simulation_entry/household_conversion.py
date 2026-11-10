"""Stored households, in the situation shape the household functions take.

A stored household (the Public API's ``households.household_data``) lists
its people and each group entity as records, each with an ``id`` and its
``values``; each person names, for every group collection, the ``id`` of the
group it belongs to. The household functions take the situation shape:
``people`` maps each person to their values, and each group collection, by
its plural, maps each group to its ``members`` and values. Records keep
their ``id`` as their name, so a calculated value maps back to its record.
The saved SPM selection is the document's ``spm``, kept apart from the
situation. Ported from sim #957.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

# Each country's stored group collections and their plural in the situation,
# as the Public API's households service accepts them.
GROUP_COLLECTIONS: Final[dict[str, dict[str, str]]] = {
    "us": {
        "household": "households",
        "family": "families",
        "tax_unit": "tax_units",
        "spm_unit": "spm_units",
        "marital_unit": "marital_units",
    },
    "uk": {"household": "households", "benunit": "benunits"},
}


class UnreadableHouseholdError(ValueError):
    """A stored household document lacks a field the Public API always stores."""


def household_situation(country: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stored household's people and groups in the situation shape.

    A group no person belongs to keeps an empty member list, which the
    household functions' contract refuses. Raise UnreadableHouseholdError
    for a document without the records the Public API stores.
    """
    try:
        people = document["people"]
        situation: dict[str, Any] = {
            "people": {person["id"]: dict(person["values"]) for person in people}
        }
        for collection, plural in GROUP_COLLECTIONS[country].items():
            situation[plural] = {
                group["id"]: {
                    "members": [
                        person["id"]
                        for person in people
                        if person["memberships"].get(collection) == group["id"]
                    ],
                    **group["values"],
                }
                for group in document.get(collection, [])
            }
    except (KeyError, TypeError, AttributeError) as error:
        msg = f"the stored household's people or groups cannot be read: {error!r}"
        raise UnreadableHouseholdError(msg) from None
    return situation


def saved_spm_selection(document: Mapping[str, Any]) -> object:
    """Return the household's saved SPM selection, or None when it has none.

    A saved null selection is no choice, as the household contract reads it.
    """
    return document.get("spm")
