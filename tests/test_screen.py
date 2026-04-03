import asyncio
from pathlib import Path

import screen


def test_prepare_image_for_vision_keeps_small_file(tmp_path):
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"x" * (screen.VISION_TARGET_IMAGE_BYTES - 10))

    result = asyncio.run(screen._prepare_image_for_vision(image_path))

    assert result == image_path


def test_prepare_image_for_vision_compresses_large_file(tmp_path, monkeypatch):
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"x" * (screen.VISION_TARGET_IMAGE_BYTES + 10))

    async def fake_transform(input_path: Path, output_path: Path, max_pixels: int | None, quality: int) -> bool:
        output_path.write_bytes(b"y" * 1024)
        return True

    monkeypatch.setattr(screen, "_run_image_transform", fake_transform)

    result = asyncio.run(screen._prepare_image_for_vision(image_path))

    assert result == tmp_path / "screen.vision.jpg"
    assert result.exists()
    assert result.stat().st_size == 1024
