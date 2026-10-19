"""GSUB/GPOS introspection: which characters must live in the same shard.

A per-shard subsetting closure pulls in whatever glyphs a layout rule needs, but it
does so *independently per shard*: a rule whose operands end up in different shards
is either duplicated or broken. This module extracts the character sets that must be
co-located.

Granularity matters as much as correctness here. A whole-lookup union is far too
coarse: `ccmp`/`locl`/`vert` single substitutions cover every glyph in a CJK font, so
unioning them would collapse the whole partition into one giant shard. Rules are
therefore extracted one by one:

* single substitutions and single positioning are *self contained* - each character
  substitutes to its own variant glyph and needs nothing from anybody else;
* ligatures, multiple/alternate substitutions and pair positioning give one group per
  rule (a ligature, a mapping entry, a kerning pair);
* everything else (class based rules, context formats 2/3, marks, cursive) yields one
  group per subtable, which is dropped when it exceeds `MAX_GROUP_CHARS` - the
  per-shard closure still carries the needed glyphs, at the cost of duplication.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from fontTools.ttLib import TTFont

LOOKUP_MODES = ("lookup", "feature", "none")
MAX_GROUP_CHARS = 128
_SKIP_ATTRIBUTES = frozenset({"font", "reader", "ttFont"})


@dataclass(frozen=True, slots=True)
class Constraint:
    """Characters that must stay together, plus how many dependency glyphs have no cmap entry."""

    source: str
    chars: frozenset[int]
    unmapped: int = 0
    rule: str = ""


@dataclass(frozen=True, slots=True)
class ConstraintReport:
    constraints: tuple[Constraint, ...] = ()
    rules: int = 0
    skipped: int = 0

    def __len__(self) -> int:
        return len(self.constraints)


def restrict_constraints(
    constraints: Sequence[Constraint], allowed: Iterable[int]
) -> tuple[Constraint, ...]:
    """Drop codepoints this origin does not ship.

    Co-location is per source font. A fallback rule may name characters that
    also exist in the primary cmap; those are subset from the primary font and
    are not in any fallback shard. Pairs that span origins cannot share a shard,
    and the browser does not shape across ``@font-face`` files, so they are not
    constraints. Fewer than two remaining characters cannot require co-location.
    """
    allowed_set = set(allowed)
    kept: list[Constraint] = []
    for constraint in constraints:
        chars = frozenset(code for code in constraint.chars if code in allowed_set)
        if len(chars) < 2:
            continue
        if chars == constraint.chars:
            kept.append(constraint)
            continue
        kept.append(
            Constraint(
                source=constraint.source,
                chars=chars,
                unmapped=constraint.unmapped,
                rule=constraint.rule,
            )
        )
    return tuple(kept)


def collect_constraints(
    font: TTFont,
    *,
    features: tuple[str, ...] | None,
    mode: str = "lookup",
    max_group_chars: int = MAX_GROUP_CHARS,
) -> ConstraintReport:
    """Collect co-location constraints from GSUB and GPOS.

    `features` limits the work to the lookups reachable from those feature tags
    (`None` = every feature); lookups of dropped features carry no constraint, since
    shards are subset with those features removed anyway. Constraints come back in
    lookup order, which keeps the merge deterministic.
    """
    if mode not in LOOKUP_MODES:
        raise ValueError(
            f"unknown dependency mode {mode!r} (expected one of {LOOKUP_MODES})"
        )
    if mode == "none":
        return ConstraintReport()

    reverse = _reverse_cmap(font)
    glyph_names = frozenset(font.getGlyphOrder())
    walker = _GlyphWalker(glyph_names)
    constraints: list[Constraint] = []
    seen: set[frozenset[int]] = set()
    rules = 0
    skipped = 0

    for table_tag in ("GSUB", "GPOS"):
        table = font.get(table_tag)
        if table is None:
            continue
        # Lazily loaded OT tables hand out `{reader, font}` shells until decompiled;
        # walking those would descend into the whole font.
        ensure = getattr(table.table, "ensureDecompiled", None)
        if ensure is not None:
            ensure(recurse=True)
        for source, lookups in _select_lookups(table.table, table_tag, features, mode):
            for lookup in lookups:
                for rule_label, group in _lookup_groups(lookup, walker):
                    rules += 1
                    glyphs: set[str] = set()
                    for item in group:
                        if isinstance(item, str):
                            if item in glyph_names:
                                glyphs.add(item)
                        else:
                            glyphs |= walker.collect(item)
                    chars = frozenset(
                        reverse[name] for name in glyphs if name in reverse
                    )
                    if len(chars) < 2:
                        continue
                    if len(chars) > max_group_chars:
                        skipped += 1
                        continue
                    if chars in seen:
                        continue
                    seen.add(chars)
                    constraints.append(
                        Constraint(
                            source=source,
                            chars=chars,
                            unmapped=len(glyphs) - len(chars),
                            rule=rule_label,
                        )
                    )
    return ConstraintReport(
        constraints=tuple(constraints), rules=rules, skipped=skipped
    )


# -- lookup selection --------------------------------------------------
def _reverse_cmap(font: TTFont) -> dict[str, int]:
    """glyph name -> lowest codepoint mapping to it (deterministic)."""
    reverse: dict[str, int] = {}
    for code, name in font.getBestCmap().items():
        current = reverse.get(name)
        if current is None or code < current:
            reverse[name] = code
    return reverse


def _select_lookups(
    table: object,
    table_tag: str,
    features: tuple[str, ...] | None,
    mode: str,
) -> list[tuple[str, list[object]]]:
    """Return `(source label, lookups)` pairs to analyse, in lookup order."""
    lookup_list = getattr(table, "LookupList", None)
    lookups = list(getattr(lookup_list, "Lookup", None) or [])
    if not lookups:
        return []

    if features is None:
        return [
            (f"{table_tag}/lookup#{index}", [lookup])
            for index, lookup in enumerate(lookups)
        ]

    feature_list = getattr(table, "FeatureList", None)
    records = getattr(feature_list, "FeatureRecord", None) or []
    by_feature: dict[str, list[int]] = {}
    for record in records:
        if record.FeatureTag in features:
            by_feature.setdefault(record.FeatureTag, []).extend(
                record.Feature.LookupListIndex
            )
    if not by_feature:
        return []

    if mode == "feature":
        return [
            (f"{table_tag}/{tag}", [lookups[index] for index in dict.fromkeys(indices)])
            for tag, indices in by_feature.items()
        ]

    indices = sorted({index for values in by_feature.values() for index in values})
    return [(f"{table_tag}/lookup#{index}", [lookups[index]]) for index in indices]


# -- rule extraction ---------------------------------------------------
def _lookup_groups(
    lookup: object, walker: _GlyphWalker
) -> Iterator[tuple[str, list[Any]]]:
    """Yield `(rule label, items)` for one lookup; items are glyph names or objects."""
    subtables = lookup.SubTable
    if not isinstance(subtables, list):
        subtables = [subtables]
    for subtable in subtables:
        for group in _subtable_groups(subtable):
            if group:
                yield f"{type(subtable).__name__}", group


def _subtable_groups(subtable: object) -> list[list[Any]]:
    """Split one subtable into rule groups (see the module docstring for the policy)."""
    handler = _RULES.get(type(subtable).__name__)
    if handler is not None:
        return handler(subtable)
    return [[subtable]]


def _self_contained(subtable: object) -> list[list[Any]]:
    """A 1:1 substitution/positioning rule needs no co-location with other characters."""
    del subtable
    return []


def _extension(subtable: object) -> list[list[Any]]:
    return _subtable_groups(subtable.ExtSubTable)


def _mapping_rules(attribute: str):
    def handler(subtable: object) -> list[list[Any]]:
        mapping = getattr(subtable, attribute)
        groups = []
        for source, value in mapping.items():
            targets = value if isinstance(value, list) else [value]
            groups.append([source, *targets])
        return groups

    return handler


def _ligatures(subtable: object) -> list[list[Any]]:
    return [
        [first, *ligature.Component, ligature.LigGlyph]
        for first, ligatures in subtable.ligatures.items()
        for ligature in ligatures
    ]


def _pair_pos_format1(subtable: object) -> list[list[Any]]:
    groups = []
    for first, pair_set in zip(subtable.Coverage.glyphs, subtable.PairSet):
        for record in pair_set.PairValueRecord:
            groups.append([first, record.SecondGlyph])
    return groups


def _pair_pos_format2(subtable: object) -> list[list[Any]]:
    """Class based pair positioning: one group per non-empty class pair (usually capped out)."""
    groups = []
    class1 = subtable.ClassDef1.classDefs
    class2 = subtable.ClassDef2.classDefs
    for index, record1 in enumerate(subtable.Class1Record):
        for class2_index, record2 in enumerate(record1.Class2Record):
            if (
                getattr(record2, "Value1", None) is None
                and getattr(record2, "Value2", None) is None
            ):
                continue
            groups.append(
                _class_glyphs(class1, index) + _class_glyphs(class2, class2_index)
            )
    return groups


def _context(subtable: object) -> list[list[Any]]:
    """Contextual rules: exact groups for format 1, one capped group otherwise.

    Format 1 spells its sequences out as glyph names, so each rule is small and exact.
    Formats 2 (classes) and 3 (per position coverages) would need class to glyph
    expansion for every position; they fall back to a single group.
    """
    rule_sets = _first_attribute(
        subtable, "SubRuleSet", "PosRuleSet", "ChainSubRuleSet", "ChainPosRuleSet"
    )
    if rule_sets is None:
        return [[subtable]]

    coverage = _coverage_list(getattr(subtable, "Coverage", None))
    groups: list[list[Any]] = []
    for index, rule_set in enumerate(rule_sets):
        if rule_set is None:
            continue
        rules = (
            _first_attribute(
                rule_set, "SubRule", "PosRule", "ChainSubRule", "ChainPosRule"
            )
            or []
        )
        first = coverage[index] if index < len(coverage) else None
        for rule in rules:
            group: list[Any] = [first] if first else []
            group.append(rule)
            groups.append(group)
    return groups or [[subtable]]


def _mark(subtable: object) -> list[list[Any]]:
    """Mark attachment: marks and their base/ligature/mark2 partners must co-locate."""
    items: list[Any] = []
    for attribute in (
        "MarkCoverage",
        "BaseCoverage",
        "LigatureCoverage",
        "Mark2Coverage",
    ):
        coverage = getattr(subtable, attribute, None)
        if coverage is not None:
            items.append(coverage)
    return [items] if items else [[subtable]]


def _cursive(subtable: object) -> list[list[Any]]:
    return (
        [[subtable.Coverage]] if getattr(subtable, "Coverage", None) else [[subtable]]
    )


def _reverse_chain(subtable: object) -> list[list[Any]]:
    groups = []
    coverage = getattr(subtable, "Coverage", None)
    substitutes = getattr(subtable, "Substitute", None) or []
    for index, glyph in enumerate(_coverage_list(coverage)):
        group: list[Any] = [glyph]
        if index < len(substitutes):
            group.append(substitutes[index])
        for attribute in ("BacktrackCoverage", "LookAheadCoverage"):
            group.extend(_coverage_list(getattr(subtable, attribute, None)))
        groups.append(group)
    return groups or [[subtable]]


_RULES = {
    "SingleSubst": _self_contained,
    "SinglePos": _self_contained,
    "MultipleSubst": _mapping_rules("mapping"),
    "AlternateSubst": _mapping_rules("alternates"),
    "LigatureSubst": _ligatures,
    "PairPos": lambda subtable: (
        _pair_pos_format1(subtable)
        if hasattr(subtable, "PairSet")
        else _pair_pos_format2(subtable)
    ),
    "ContextSubst": _context,
    "ContextPos": _context,
    "ChainContextSubst": _context,
    "ChainContextPos": _context,
    "ReverseChainSingleSubst": _reverse_chain,
    "MarkBasePos": _mark,
    "MarkLigPos": _mark,
    "MarkMarkPos": _mark,
    "CursivePos": _cursive,
    "ExtensionSubst": _extension,
    "ExtensionPos": _extension,
}


# -- small helpers -----------------------------------------------------
def _first_attribute(obj: object, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _coverage_list(coverage: Any) -> list[str]:
    """Normalise a Coverage object (or a list of them, format 3) into glyph names."""
    if coverage is None:
        return []
    if isinstance(coverage, list):
        return [name for item in coverage for name in _coverage_list(item)]
    return list(getattr(coverage, "glyphs", []) or [])


def _class_glyphs(class_def: dict[str, int], index: int) -> list[str]:
    return [name for name, value in class_def.items() if value == index]


class _GlyphWalker:
    """Collects every glyph name referenced inside one rule object.

    fontTools stores glyphs as *names* in every subtable format (coverage, class
    definitions, ligature components, substitutes, pair records), so recursively
    visiting the object graph and keeping any string that is in the font's glyph order
    covers all formats, including extension wrappers, without a hand maintained table
    of formats.
    """

    __slots__ = ("glyphs", "names", "seen")

    def __init__(self, names: frozenset[str]) -> None:
        self.names = names
        self.glyphs: set[str] = set()
        self.seen: set[int] = set()

    def collect(self, obj: object) -> set[str]:
        self.glyphs = set()
        self.seen = set()
        self._visit(obj, 0)
        return self.glyphs

    def _visit(self, obj: object, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(obj, str):
            if obj in self.names:
                self.glyphs.add(obj)
            return
        if isinstance(obj, (bytes, bytearray, int, float, bool, type(None))):
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                self._visit(key, depth + 1)
                self._visit(value, depth + 1)
            return
        if isinstance(obj, (list, tuple, set, frozenset)):
            # Rules share and re-reference their objects; walking the same node twice
            # can blow up combinatorially and cannot add any glyph name.
            if id(obj) in self.seen:
                return
            self.seen.add(id(obj))
            for item in obj:
                self._visit(item, depth + 1)
            return
        attributes = getattr(obj, "__dict__", None)
        if attributes:
            key = id(obj)
            if key in self.seen:
                return
            self.seen.add(key)
            if "reader" in attributes:
                ensure = getattr(obj, "ensureDecompiled", None)
                if ensure is not None:
                    ensure()
                    attributes = obj.__dict__
            for name, value in attributes.items():
                # Never follow a table back to its font or reader: from there the walk
                # reaches every glyph in the font.
                if name in _SKIP_ATTRIBUTES:
                    continue
                self._visit(value, depth + 1)


def constraint_chars(constraints: Sequence[Constraint]) -> set[int]:
    """All characters mentioned by any constraint (diagnostics)."""
    out: set[int] = set()
    for constraint in constraints:
        out |= constraint.chars
    return out
