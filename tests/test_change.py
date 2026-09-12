import hashlib

import pytest

from pagewatcher.change import ChangeReason, assess_change


def test_first_snapshot_establishes_a_baseline() -> None:
    result = assess_change(None, "In stock")

    assert result.has_changed is False
    assert result.is_material is False
    assert result.reason is ChangeReason.BASELINE
    assert result.previous_hash is None
    assert result.similarity is None
    assert result.changed_characters == 0


def test_identical_snapshot_is_unchanged() -> None:
    result = assess_change("In stock", "In stock")

    assert result.has_changed is False
    assert result.is_material is False
    assert result.reason is ChangeReason.IDENTICAL
    assert result.similarity == 1.0
    assert result.changed_characters == 0


def test_hashes_are_stable_sha256_digests() -> None:
    result = assess_change("old", "new")

    assert result.previous_hash == hashlib.sha256(b"old").hexdigest()
    assert result.current_hash == hashlib.sha256(b"new").hexdigest()


def test_small_relative_and_absolute_change_is_not_material() -> None:
    previous = "a" * 99 + "x"
    current = "a" * 99 + "y"

    result = assess_change(
        previous,
        current,
        similarity_threshold=0.98,
        minimum_changed_characters=20,
    )

    assert result.has_changed is True
    assert result.is_material is False
    assert result.reason is ChangeReason.BELOW_THRESHOLDS
    assert result.similarity == 0.99
    assert result.changed_characters == 1


def test_large_relative_change_is_material_even_when_short() -> None:
    result = assess_change(
        "In stock",
        "Sold out",
        similarity_threshold=0.8,
        minimum_changed_characters=20,
    )

    assert result.is_material is True
    assert result.reason is ChangeReason.SIMILARITY
    assert result.similarity is not None and result.similarity < 0.8
    assert result.changed_characters < 20


def test_large_absolute_change_is_material_even_when_similarity_is_high() -> None:
    previous = "a" * 2_000
    current = "a" * 1_000 + "b" * 25 + "a" * 1_000

    result = assess_change(
        previous,
        current,
        similarity_threshold=0.98,
        minimum_changed_characters=20,
    )

    assert result.is_material is True
    assert result.reason is ChangeReason.CHARACTER_COUNT
    assert result.similarity is not None and result.similarity > 0.98
    assert result.changed_characters == 25


def test_change_can_cross_both_thresholds() -> None:
    result = assess_change(
        "a" * 25,
        "b" * 25,
        similarity_threshold=0.98,
        minimum_changed_characters=20,
    )

    assert result.is_material is True
    assert result.reason is ChangeReason.BOTH_THRESHOLDS
    assert result.similarity == 0.0
    assert result.changed_characters == 25


def test_similarity_threshold_is_exclusive() -> None:
    result = assess_change(
        "abcd",
        "abce",
        similarity_threshold=0.75,
        minimum_changed_characters=10,
    )

    assert result.similarity == 0.75
    assert result.is_material is False


def test_character_threshold_is_inclusive() -> None:
    result = assess_change(
        "a" * 100,
        "a" * 95 + "b" * 5,
        similarity_threshold=0.9,
        minimum_changed_characters=5,
    )

    assert result.changed_characters == 5
    assert result.is_material is True
    assert result.reason is ChangeReason.CHARACTER_COUNT


def test_empty_content_can_be_a_material_change() -> None:
    result = assess_change(
        "Available",
        "",
        similarity_threshold=0.98,
        minimum_changed_characters=20,
    )

    assert result.has_changed is True
    assert result.is_material is True
    assert result.reason is ChangeReason.SIMILARITY


@pytest.mark.parametrize("threshold", [-0.01, 1.01])
def test_rejects_invalid_similarity_threshold(threshold: float) -> None:
    with pytest.raises(ValueError, match="similarity_threshold"):
        assess_change("old", "new", similarity_threshold=threshold)


@pytest.mark.parametrize("minimum", [0, -1])
def test_rejects_invalid_character_threshold(minimum: int) -> None:
    with pytest.raises(ValueError, match="minimum_changed_characters"):
        assess_change("old", "new", minimum_changed_characters=minimum)
