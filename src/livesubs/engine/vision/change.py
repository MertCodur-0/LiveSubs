"""Subtitle change detection on text masks.

Only the text mask is compared, never raw pixels: a moving, flashing background does not
change the mask, a new line does. Masks are shrunk to a small thumbnail first, so 1-2 px
jitter and anti-aliasing noise disappear before the comparison.
"""

from __future__ import annotations

from enum import StrEnum

import cv2
import numpy as np

THUMB_WIDTH = 96


class Change(StrEnum):
    NONE = "none"  # no subtitle, still none
    SAME = "same"  # the same subtitle is still up
    APPEARED = "appeared"  # a subtitle showed up where there was none
    CHANGED = "changed"  # a different subtitle replaced the previous one
    CLEARED = "cleared"  # the subtitle disappeared


def thumbnail(mask: np.ndarray) -> np.ndarray:
    """Boolean mask shrunk to ``THUMB_WIDTH`` wide (cells at least a quarter covered)."""
    height = max(4, round(THUMB_WIDTH * mask.shape[0] / mask.shape[1]))
    small = cv2.resize(mask, (THUMB_WIDTH, height), interpolation=cv2.INTER_AREA)
    return small >= 64


def iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.count_nonzero(a | b)
    return np.count_nonzero(a & b) / union if union else 1.0


class ChangeDetector:
    """Feed it one mask per read; it says what happened since the previous one."""

    def __init__(self, min_text_fraction: float = 0.001, min_iou: float = 0.75) -> None:
        """``min_text_fraction``: mask coverage below which the region counts as empty.
        ``min_iou``: thumbnail overlap at or above which two masks show the same subtitle.
        """
        self.min_text_fraction = min_text_fraction
        self.min_iou = min_iou
        self._previous: np.ndarray | None = None

    def reset(self) -> None:
        self._previous = None

    def has_text(self, mask: np.ndarray) -> bool:
        return cv2.countNonZero(mask) >= self.min_text_fraction * mask.size

    def update(self, mask: np.ndarray) -> Change:
        current = thumbnail(mask) if self.has_text(mask) else None
        previous, self._previous = self._previous, current
        if current is None:
            return Change.NONE if previous is None else Change.CLEARED
        if previous is None:
            return Change.APPEARED
        if previous.shape != current.shape or iou(previous, current) < self.min_iou:
            return Change.CHANGED
        return Change.SAME
