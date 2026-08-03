"""Legacy set-name reconciliation.

Inventory added before the catalog moved to TCGdex carries older set names — from
pokemontcg.io, from tcgcsv autofill, or with encoding artifacts — that no longer
match the catalog's current nomenclature, so set-keyed lookups (thumbnails,
prices) miss. This maps those old names to the canonical catalog names.

Keys are *normalized* set names (case/punctuation-free); values are the exact
current catalog set name. Only **genuine renames** live here — pure
formatting/punctuation differences are handled by normalized comparison at the
call site, not by an entry per variant.
"""

from __future__ import annotations


def _norm(text: str | None) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


# normalized old set name -> current catalog set name
_ALIASES: dict[str, str] = {
    # Black Star Promos: TCGdex uses the short era code; tcgcsv its own names.
    "scarletvioletblackstarpromos": "SVP Black Star Promos",
    "scarletvioletpromocards": "SVP Black Star Promos",
    "megaevolutionpromo": "MEP Black Star Promos",
    "megaevolutionpromos": "MEP Black Star Promos",
    "swshswordshieldpromocards": "SWSH Black Star Promos",
    "smpromos": "SM Black Star Promos",
    "xypromos": "XY Black Star Promos",
    # 151 (pokemontcg / tcgcsv / JP naming) -> the catalog's "151".
    "pokemoncard151": "151",
    "scarletviolet151": "151",
    "svscarletviolet151": "151",
    # Base / subset / era renames.
    "scarletvioletbaseset": "Scarlet & Violet",
    "xyevolutions": "Evolutions",
    "base": "Base Set",
    "celebrationsclassiccollection": "Celebrations Classic Collection",
    "legendarytreasuresradiantcollection": "Legendary Treasures",
    "extrainerkitlatias": "EX trainer Kit (Latias)",
    # HeartGold & SoulSilver era — the collector app stored these with a mangled
    # em-dash ("HS<?>Triumphant"); the era prefix isn't in the catalog name.
    "hstriumphant": "Triumphant",
    "hsundaunted": "Undaunted",
    "hsunleashed": "Unleashed",
}


def canonical_set_name(name: str | None) -> str | None:
    """The current catalog set name for a legacy/aliased ``name``, or ``None`` if
    ``name`` isn't a known old nomenclature (already-canonical names return
    ``None`` — nothing to change)."""
    return _ALIASES.get(_norm(name))
