"""BT-D48B superseded by BT-D49 box-only mode — markers no longer used."""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="BT-D49 replaced marker/tick rendering with short position boxes")
def test_marker_geometry_not_sub_pixel_arrow():
    pass
