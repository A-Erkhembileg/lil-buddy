#!/usr/bin/env python3
"""Export the app's natural animal artwork without opening the desktop pet.

    .venv/bin/python -B tools/export_sprite_sheet.py

The presentation sheet has a solid background. The matching transparent atlas
and JSON contain native 60 x 48 frames ready for another pixel-art workflow.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


CELL = (60, 48)
BACKGROUND = "#deded7"
LABEL = "#414744"
MUTED = "#727974"
DIGITS = {
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
}
FONT = {**pet.PIXEL_FONT, **DIGITS}


def bitmap_text(image: Image.Image, text: str, position: tuple[int, int],
                scale: int = 1, color: str = LABEL) -> None:
    """Use the same tiny alphabet as the pet, with no external font dependency."""
    draw = ImageDraw.Draw(image)
    x, y = position
    for index, character in enumerate(text.upper()):
        glyph = FONT.get(character, FONT["?"])
        for row, bits in enumerate(glyph):
            for column in range(5):
                if bits & (1 << (4 - column)):
                    px, py = x + (index * 6 + column) * scale, y + row * scale
                    draw.rectangle((px, py, px + scale - 1, py + scale - 1), fill=color)


def render_frame(animal: str, pose: str, frame: int) -> Image.Image:
    pixmap = pet.DRAWERS[animal](pose, frame, 1, palette=pet.PALETTES["Natural Coat"])
    image = pixmap.toImage().scaled(*CELL, Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.FastTransformation)
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = image.constBits().asstring(image.sizeInBytes())
    return Image.frombytes("RGBA", CELL, raw, "raw", "RGBA", image.bytesPerLine()).copy()


def specifications() -> list[tuple[str, list[tuple[str, int, str, int]]]]:
    """Each tuple is pose, frame index, presentation caption, duration in ms."""
    crow = [("rest", 0, "PERCH", 600)]
    crow += [("fly", index, f"FLIGHT {index + 1:02}", 55) for index in range(8)]
    rows = [("Crow", crow)]
    for animal in ("Dog", "Fox"):
        frames = [("rest", 0, "REST", 600)]
        frames += [("walk", index, f"WALK {index + 1:02}", 90) for index in range(6)]
        frames += [("petted", 0, "PETTED", 90), ("sleep", 0, "SLEEP", 800)]
        rows.append((animal, frames))
    return rows


def export(output: Path, scale: int = 3) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=True)
    rows = specifications()
    frames = {(animal, pose, frame): render_frame(animal, pose, frame)
              for animal, sequence in rows for pose, frame, _, _ in sequence}

    # A compact raw atlas: three equal rows, nine exact native frames per row.
    atlas = Image.new("RGBA", (CELL[0] * 9, CELL[1] * 3))
    manifest = {
        "format_version": 1,
        "image": "animal-sprites-atlas.png",
        "cell_size": list(CELL),
        "direction": "right",
        "coat": "natural",
        "animals": {},
    }
    for row, (animal, sequence) in enumerate(rows):
        clips = {}
        for column, (pose, index, _, milliseconds) in enumerate(sequence):
            atlas.alpha_composite(frames[animal, pose, index], (column * CELL[0], row * CELL[1]))
            clips.setdefault(pose, []).append({
                "frame": index, "rect": [column * CELL[0], row * CELL[1], *CELL],
                "duration_ms": milliseconds,
            })
        manifest["animals"][animal.lower()] = clips
    atlas_path = output / "animal-sprites-atlas.png"
    atlas.save(atlas_path)
    manifest_path = output / "animal-sprites-atlas.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    sprite_width, sprite_height = CELL[0] * scale, CELL[1] * scale
    margin, gap = 24, 12
    tile_width = sprite_width + gap
    title_height, row_header, caption_height, row_gap = 52, 28, 18, 26
    row_height = row_header + sprite_height + caption_height + row_gap
    sheet = Image.new("RGB", (margin * 2 + tile_width * 9 - gap,
                               title_height + row_height * 3 + 8), BACKGROUND)
    bitmap_text(sheet, "NATURAL ANIMAL SPRITES", (margin, 17), 2)
    headers = {"Crow": "CROW - EIGHT FRAME FLIGHT", "Dog": "DOG - SIX FRAME WALK",
               "Fox": "FOX - SIX FRAME WALK"}
    for row, (animal, sequence) in enumerate(rows):
        top = title_height + row * row_height
        bitmap_text(sheet, headers[animal], (margin, top + 2), 2)
        for column, (pose, index, caption, _) in enumerate(sequence):
            x = margin + column * tile_width
            sprite = frames[animal, pose, index].resize((sprite_width, sprite_height), Image.Resampling.NEAREST)
            sheet.paste(sprite, (x, top + row_header), sprite)
            caption_width = len(caption) * 6 - 1
            bitmap_text(sheet, caption, (x + (sprite_width - caption_width) // 2,
                                         top + row_header + sprite_height + 8), 1, MUTED)
    sheet_path = output / "animal-sprite-sheet.png"
    sheet.save(sheet_path)

    strip = Image.new("RGBA", (CELL[0] * 8, CELL[1]))
    gif_frames = []
    gif_scale = 5
    gif_size = (CELL[0] * gif_scale + 80, CELL[1] * gif_scale + 48)
    for index in range(8):
        sprite = frames["Crow", "fly", index]
        strip.alpha_composite(sprite, (index * CELL[0], 0))
        enlarged = sprite.resize((CELL[0] * gif_scale, CELL[1] * gif_scale), Image.Resampling.NEAREST)
        canvas = Image.new("RGB", gif_size, BACKGROUND)
        canvas.paste(enlarged, (40, 24), enlarged)
        gif_frames.append(canvas)
    strip_path = output / "crow-flight-strip.png"
    strip.save(strip_path)
    gif_path = output / "crow-flight.gif"
    # GIF stores centiseconds: alternating 50/60 ms preserves the app's 55 ms mean.
    gif_frames[0].save(gif_path, save_all=True, append_images=gif_frames[1:], duration=[50, 60] * 4,
                       loop=0, disposal=2, optimize=False)
    return {"sheet": str(sheet_path), "atlas": str(atlas_path), "manifest": str(manifest_path),
            "crow_flight_strip": str(strip_path), "crow_flight_preview": str(gif_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artwork")
    parser.add_argument("--scale", type=int, choices=(2, 3, 4), default=3)
    options = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    paths = export(options.output.resolve(), options.scale)
    app.processEvents()
    print(json.dumps(paths, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
