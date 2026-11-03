#!/usr/bin/env python3
"""Bake supplied 4x4 motion sheets into registered runtime atlases.

    .venv/bin/python -B tools/bake_motion_atlases.py --animal cat dog

This is build tooling. It samples supplied artwork without drawing characters,
and only Pillow and Qt are needed. Native app launches do not run the baker.
"""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import desktop_cat as pet


CELL_SIZE = (400, 320)
ALPHA_THRESHOLD = 80


def pil_image(image: QImage) -> Image.Image:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (image.width(), image.height()),
                           image.constBits().asstring(image.sizeInBytes()),
                           "raw", "RGBA", image.bytesPerLine()).copy()


def _clean_component(image: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Keep the main opaque animal and its two-pixel antialiased fringe."""
    image = image.convert("RGBA")
    width, height = image.size
    alpha = image.getchannel("A").tobytes()
    seen = bytearray(width * height)
    largest: list[int] = []
    for start, value in enumerate(alpha):
        if value < ALPHA_THRESHOLD or seen[start]:
            continue
        seen[start] = 1
        queue, component = deque([start]), []
        while queue:
            pos = queue.popleft()
            component.append(pos)
            x, y = pos % width, pos // width
            neighbours = []
            if x:
                neighbours.append(pos-1)
            if x+1 < width:
                neighbours.append(pos+1)
            if y:
                neighbours.append(pos-width)
            if y+1 < height:
                neighbours.append(pos+width)
            for other in neighbours:
                if not seen[other] and alpha[other] >= ALPHA_THRESHOLD:
                    seen[other] = 1
                    queue.append(other)
        if len(component) > len(largest):
            largest = component
    if not largest:
        raise ValueError("A supplied motion cell contains no opaque animal.")
    keep = bytearray(width * height)
    for pos in largest:
        keep[pos] = 255
    mask = Image.frombytes("L", image.size, bytes(keep)).filter(ImageFilter.MaxFilter(5))
    keep = mask.tobytes()
    raw = bytearray(image.tobytes())
    for pos, value in enumerate(alpha):
        # Invisible RGB noise is not part of the artwork or the silhouette.
        if not keep[pos] or value < 4:
            raw[pos*4:pos*4+4] = b"\0\0\0\0"
    clean = Image.frombytes("RGBA", image.size, bytes(raw))
    bounds = clean.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("Motion-cell cleanup removed the entire animal.")
    return clean, bounds


def read_sheet(path: Path) -> tuple[list[dict], tuple[int, int]]:
    with Image.open(path) as supplied:
        sheet = supplied.convert("RGBA")
    width, height = sheet.size
    if width < 128 or height < 128:
        raise ValueError(f"Motion sheet is too small for sixteen frames: {path}")
    cells = []
    for row in range(4):
        for column in range(4):
            # E.g. a 1254px image has alternating 313/314px cells.
            rectangle = (round(column*width/4), round(row*height/4),
                         round((column+1)*width/4), round((row+1)*height/4))
            image, bounds = _clean_component(sheet.crop(rectangle))
            cells.append({"image": image, "bounds": bounds,
                          "source_rect": list(rectangle), "source_cell": row*4+column})
    return cells, sheet.size


def _anchors(image: Image.Image, bounds: tuple[int, int, int, int]) -> tuple[float, float, float]:
    """Use upper-right face features and lower body, excluding the trailing tail."""
    left, top, right, bottom = bounds
    width, height = right-left, bottom-top
    rgba = image.load()
    # The authored sheets face right. This region excludes tail, feet and ear tips.
    face = (round(left+width*0.54), round(top+height*0.19),
            round(right-width*0.025), round(top+height*0.62))
    candidates = []
    for y in range(face[1], face[3]):
        for x in range(face[0], face[2]):
            red, green, blue, alpha = rgba[x, y]
            if alpha >= 160:
                luminance = (red*54+green*183+blue*19)//256
                candidates.append((luminance, x, y))
    if candidates:
        ordered = sorted(value[0] for value in candidates)
        cutoff = ordered[max(0, round((len(ordered)-1)*0.20))]
        dark = [(x, y, max(1, cutoff+1-lum)) for lum, x, y in candidates if lum <= cutoff]
        weight = sum(item[2] for item in dark)
        head_x = sum(x*w for x, _y, w in dark)/weight
        head_y = sum(y*w for _x, y, w in dark)/weight
    else:
        head_x, head_y = left+width*0.79, top+height*0.38
    feet = []
    for x in range(round(left+width*0.28), round(left+width*0.94)):
        for y in range(bottom-1, round(top+height*0.50)-1, -1):
            if rgba[x, y][3] >= ALPHA_THRESHOLD:
                feet.append(y)
                break
    feet.sort()
    ground = feet[min(len(feet)-1, round((len(feet)-1)*0.92))] if feet else bottom-1
    return head_x, head_y, float(ground)


def register_clip(cells: list[dict], clip: str) -> tuple[list[Image.Image], dict]:
    """Register the character independently of swinging tails and lifted paws."""
    anchors = [_anchors(cell["image"], cell["bounds"]) for cell in cells]
    center_x = statistics.median(item[0] for item in anchors)
    center_y = statistics.median(item[1] for item in anchors)
    ground_y = statistics.median(item[2] for item in anchors)
    median_width = statistics.median(cell["image"].width for cell in cells)
    median_height = statistics.median(cell["image"].height for cell in cells)
    placements = []
    for cell, (head_x, head_y, ground) in zip(cells, anchors):
        # Horizontal registration tracks the head, not a changing tail silhouette.
        dx = center_x-head_x
        dx = max(-median_width*0.17, min(median_width*0.17, dx))
        if clip == "air":
            # Keep a little authored head bob while removing large cell offsets.
            delta = head_y-center_y
            dy = -delta+max(-2.0, min(2.0, delta))
        else:
            delta = ground-ground_y
            # Small differences represent the gait's paw lifts and body bounce.
            # Correct substantial whole-cell placement errors without flattening it.
            allowed = median_height*0.022
            dy = 0.0 if abs(delta) <= allowed else -delta+max(-allowed, min(allowed, delta))
        placements.append((dx, dy))
    left = min(cell["bounds"][0]+dx for cell, (dx, _dy) in zip(cells, placements))
    top = min(cell["bounds"][1]+dy for cell, (_dx, dy) in zip(cells, placements))
    right = max(cell["bounds"][2]+dx for cell, (dx, _dy) in zip(cells, placements))
    bottom = max(cell["bounds"][3]+dy for cell, (_dx, dy) in zip(cells, placements))
    factor = min((CELL_SIZE[0]-18)/(right-left), (CELL_SIZE[1]-8)/(bottom-top))
    offset_x = (CELL_SIZE[0]-(right-left)*factor)/2-left*factor
    offset_y = CELL_SIZE[1]-1-bottom*factor
    frames, frame_metadata = [], []
    for cell, anchor, (dx, dy) in zip(cells, anchors, placements):
        original = cell["image"]
        sized = original.resize((max(1,round(original.width*factor)),
                                 max(1,round(original.height*factor))), Image.Resampling.LANCZOS)
        position = round(offset_x+dx*factor), round(offset_y+dy*factor)
        canvas = Image.new("RGBA", CELL_SIZE)
        canvas.alpha_composite(sized, position)
        # Resampling may create minuscule alpha ringing far from the silhouette.
        alpha = canvas.getchannel("A").point(lambda value: 0 if value < 4 else value)
        canvas.putalpha(alpha)
        canvas = Image.alpha_composite(Image.new("RGBA", CELL_SIZE), canvas)
        frames.append(canvas)
        frame_metadata.append({
            "source_cell": cell["source_cell"], "source_rect": cell["source_rect"],
            "source_bounds": list(cell["bounds"]),
            "head_anchor": [round(anchor[0],3),round(anchor[1],3)],
            "source_ground_y": round(anchor[2],3),
            "translation": [round(dx,3),round(dy,3)],
            "canvas_position": list(position),
            "canvas_head_anchor": [round(position[0]+anchor[0]*factor,3),
                                   round(position[1]+anchor[1]*factor,3)],
            "canvas_ground_y": round(position[1]+anchor[2]*factor,3),
        })
    unique = len({hashlib.sha256(frame.tobytes()).digest() for frame in frames})
    if unique != len(frames):
        raise ValueError(f"The supplied {clip} clip has {unique} distinct frames; expected {len(frames)}.")
    return frames, {"scale": factor, "source_ground_median": ground_y,
                    "canvas_ground_median": statistics.median(item["canvas_ground_y"] for item in frame_metadata),
                    "registration": "face-dark-feature-x / robust-contact-baseline-y",
                    "frames": frame_metadata}


def _legacy_frames(animal: str) -> tuple[list[Image.Image], dict[str, list[int]]]:
    legacy = getattr(pet, "_legacy_sprite_bases", None) or pet._sprite_bases
    bases, clips = legacy(animal)
    anchored = []
    for base in bases:
        image = pil_image(base)
        # Legacy rendering used to place the visible cutout on the floor at
        # playback. Bake that placement once, retaining the original pixels;
        # generated gait registration is independent and is never changed here.
        opaque = image.getchannel("A").point(lambda value: 255 if value >= ALPHA_THRESHOLD else 0)
        bounds = opaque.getbbox()
        if bounds is None:
            raise ValueError(f"The original {animal} seed contains no visible artwork.")
        offset_y = image.height-bounds[3]
        if offset_y:
            canvas = Image.new("RGBA",image.size)
            canvas.alpha_composite(image,(0,offset_y))
            image = canvas
        anchored.append(image)
    return anchored, clips


def write_contact(path: Path, frames: list[Image.Image], clips: dict[str, list[int]],
                  registration: dict) -> None:
    width, height = 200, 160
    columns = 8
    total = sum(len(indexes) for indexes in clips.values())
    rows = math.ceil(total/columns)
    sheet = Image.new("RGB", (columns*width, rows*(height+30)), "#e9e2d4")
    draw = ImageDraw.Draw(sheet)
    tile = 0
    for clip, indexes in clips.items():
        for index_in_clip, index in enumerate(indexes):
            x, y = tile%columns*width, tile//columns*(height+30)
            small = frames[index].resize((width,height), Image.Resampling.LANCZOS)
            sheet.paste(small,(x,y),small)
            if clip in registration:
                data = registration[clip]["frames"][index_in_clip]
                ax, ay = data["canvas_head_anchor"]
                ground = registration[clip]["canvas_ground_median"]
                draw.line((x+round(ax/2),y,x+round(ax/2),y+height-1), fill="#cc4466")
                draw.line((x,y+round(ground/2),x+width-1,y+round(ground/2)), fill="#4477aa")
                draw.ellipse((x+ax/2-2,y+ay/2-2,x+ax/2+2,y+ay/2+2),fill="#cc4466")
            draw.text((x+8,y+height+5), f"{clip} {index_in_clip+1:02}",fill="#393333")
            tile += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def bake(animal: str, source_root: Path, output_root: Path, preview_root: Path) -> dict:
    slug = animal.casefold()
    paths = {clip: source_root/f"{slug}-{clip}.png" for clip in ("walk", "reactions")}
    for path in paths.values():
        if not path.is_file():
            raise FileNotFoundError(f"Motion source has not arrived: {path}")
    walking, walk_size = read_sheet(paths["walk"])
    reactions, reaction_size = read_sheet(paths["reactions"])
    legacy, legacy_clips = _legacy_frames(animal)
    frames: list[Image.Image] = []
    clips: dict[str, list[int]] = {}
    registration = {}

    def append(clip: str, pictures: list[Image.Image]) -> None:
        indexes = []
        for image in pictures:
            if image.size != CELL_SIZE:
                image = image.resize(CELL_SIZE, Image.Resampling.LANCZOS)
            alpha = image.getchannel("A")
            minimum, maximum = alpha.getextrema()
            if minimum != 0 or maximum < ALPHA_THRESHOLD:
                raise ValueError(f"{animal} {clip} has no proper transparent animal silhouette.")
            indexes.append(len(frames))
            frames.append(image)
        clips[clip] = indexes

    append("rest", [legacy[legacy_clips["rest"][0]]])
    for clip, cells in (("idle",reactions[:4]),("walk",walking),
                        ("pet",reactions[4:10]),("air",reactions[10:16])):
        images, metadata = register_clip(cells,clip)
        registration[clip] = metadata
        append(clip,images)
    sleep_indexes = legacy_clips["sleep"]
    append("sleep", [legacy[sleep_indexes[min(i,len(sleep_indexes)-1)]] for i in range(2)])
    append("groom", [legacy[legacy_clips.get("groom",legacy_clips["rest"])[0]]])
    if legacy_clips.get("blink"):
        append("blink", [legacy[legacy_clips["blink"][0]]])

    columns = 8
    atlas = Image.new("RGBA", (CELL_SIZE[0]*columns,CELL_SIZE[1]*math.ceil(len(frames)/columns)))
    rectangles = []
    for index, image in enumerate(frames):
        x, y = index%columns*CELL_SIZE[0], index//columns*CELL_SIZE[1]
        atlas.alpha_composite(image,(x,y))
        rectangles.append([x,y,*CELL_SIZE])
    output_root.mkdir(parents=True,exist_ok=True)
    image_path, manifest_path = output_root/f"{slug}.png", output_root/f"{slug}.json"
    atlas.save(image_path)
    manifest = {"format_version":1,"image":image_path.name,"animal":animal,
                "cell_size":list(CELL_SIZE),"frames":rectangles,"clips":clips,
                "sources":{"walk":{"file":paths["walk"].name,"size":list(walk_size)},
                           "reactions":{"file":paths["reactions"].name,"size":list(reaction_size)}},
                "registration":registration}
    manifest_path.write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    preview = preview_root/f"{slug}-registration.png"
    write_contact(preview,frames,clips,registration)
    return {"animal":animal,"atlas":str(image_path),"manifest":str(manifest_path),
            "frames":len(frames),"clips":{clip:len(indexes) for clip,indexes in clips.items()},
            "registration_preview":str(preview)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--animal",nargs="+",choices=("cat","dog","fox"),default=("cat","dog","fox"))
    parser.add_argument("--source-root",type=Path,default=ROOT/"artwork"/"motion-sources")
    parser.add_argument("--output-root",type=Path,default=ROOT/"artwork"/"motion")
    parser.add_argument("--preview-root",type=Path,default=ROOT/"build"/"motion-registration")
    args = parser.parse_args()
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    for animal in args.animal:
        print(json.dumps(bake(animal.title(),args.source_root,args.output_root,args.preview_root),indent=2),flush=True)


if __name__ == "__main__":
    main()
