"""Classify differences between normalized page snapshots."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum


class ChangeReason(StrEnum):
    """The rule that determined a change assessment."""

    BASELINE = "baseline"
    IDENTICAL = "identical"
    BELOW_THRESHOLDS = "below_thresholds"
    SIMILARITY = "similarity"
    CHARACTER_COUNT = "character_count"
    BOTH_THRESHOLDS = "both_thresholds"


@dataclass(frozen=True, slots=True)
class ChangeAssessment:
    """A reproducible explanation of how two snapshots compare."""

    previous_hash: str | None
    current_hash: str
    similarity: float | None
    changed_characters: int
    is_material: bool
    reason: ChangeReason

    @property
    def has_changed(self) -> bool:
        """Return whether a previous snapshot exists and differs from this one."""

        return self.previous_hash is not None and self.previous_hash != self.current_hash


def assess_change(
    previous: str | None,
    current: str,
    *,
    similarity_threshold: float = 0.98,
    minimum_changed_characters: int = 20,
) -> ChangeAssessment:
    """Compare normalized snapshots and decide whether the change is material.

    A non-identical snapshot is material if its similarity is lower than the
    relative threshold, its changed-character count reaches the absolute
    threshold, or both. ``None`` represents the absence of a previous snapshot
    and establishes a baseline without reporting a change.
    """

    _validate_thresholds(similarity_threshold, minimum_changed_characters)
    current_hash = _text_hash(current)

    if previous is None:
        return ChangeAssessment(
            previous_hash=None,
            current_hash=current_hash,
            similarity=None,
            changed_characters=0,
            is_material=False,
            reason=ChangeReason.BASELINE,
        )

    previous_hash = _text_hash(previous)
    if previous_hash == current_hash:
        return ChangeAssessment(
            previous_hash=previous_hash,
            current_hash=current_hash,
            similarity=1.0,
            changed_characters=0,
            is_material=False,
            reason=ChangeReason.IDENTICAL,
        )

    matcher = SequenceMatcher(None, previous, current, autojunk=False)
    similarity = matcher.ratio()
    changed_characters = sum(
        max(previous_end - previous_start, current_end - current_start)
        for operation, previous_start, previous_end, current_start, current_end
        in matcher.get_opcodes()
        if operation != "equal"
    )

    crossed_similarity = similarity < similarity_threshold
    crossed_character_count = changed_characters >= minimum_changed_characters
    if crossed_similarity and crossed_character_count:
        reason = ChangeReason.BOTH_THRESHOLDS
    elif crossed_similarity:
        reason = ChangeReason.SIMILARITY
    elif crossed_character_count:
        reason = ChangeReason.CHARACTER_COUNT
    else:
        reason = ChangeReason.BELOW_THRESHOLDS

    return ChangeAssessment(
        previous_hash=previous_hash,
        current_hash=current_hash,
        similarity=similarity,
        changed_characters=changed_characters,
        is_material=crossed_similarity or crossed_character_count,
        reason=reason,
    )


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_thresholds(
    similarity_threshold: float, minimum_changed_characters: int
) -> None:
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0.0 and 1.0")
    if minimum_changed_characters <= 0:
        raise ValueError("minimum_changed_characters must be greater than zero")
