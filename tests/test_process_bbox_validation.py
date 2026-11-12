import pytest

from blender_mcp.generation import GenerationError, process_bbox


@pytest.mark.parametrize("bbox", ([0, 1, 1], [-1, 1, 1], [1, 1]))
def test_process_bbox_rejects_bad_boxes(bbox):
    with pytest.raises(GenerationError, match="three positive numbers"):
        process_bbox(bbox)


def test_process_bbox_scales_floats_to_percentages():
    assert process_bbox([2.0, 1.0, 0.5]) == [100, 50, 25]
    assert process_bbox([3, 2, 1]) == [3, 2, 1]
