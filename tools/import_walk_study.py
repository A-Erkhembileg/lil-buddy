#!/usr/bin/env python3
"""Replace only an animal's walking artwork with a coherent, registered study.

    python tools/import_walk_study.py --animal cat --sheet artwork/walk-studies/cat-walk.png --dry-run
    python tools/import_walk_study.py --animal cat --sheet artwork/walk-studies/cat-walk.png --subdivisions 4

One shared scale and a correction for repeated sheet-column drift preserve the stride. Each
frame's lowest visible paw is placed on y318. Other poses and their frame indexes
are retained exactly. With --subdivisions, append a smooth drawn_walk clip and
retain the original walk for older renderers. This offline tool is not imported by the desktop app.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import statistics
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from PIL import Image, ImageDraw
from tools.bake_motion_atlases import ALPHA_THRESHOLD, CELL_SIZE, _clean_component, read_sheet


FLOOR = 318
PADDING = 10


def opaque_bounds(image: Image.Image) -> tuple[int,int,int,int]:
    mask = image.getchannel("A").point(lambda value: 255 if value >= ALPHA_THRESHOLD else 0)
    bounds = mask.getbbox()
    if bounds is None:
        raise ValueError("A walking frame contains no visible animal.")
    return bounds


def load_cells(path: Path, columns: int, rows: int) -> tuple[list[dict],tuple[int,int]]:
    if min(columns,rows) <= 0:
        raise ValueError("Sheet rows and columns must be positive.")
    if (columns,rows) == (4,4):
        cells,size = read_sheet(path)
    else:
        with Image.open(path) as source:
            sheet = source.convert("RGBA")
        size = sheet.size
        cells = []
        if min(size[0]/columns,size[1]/rows) < 32:
            raise ValueError("The source grid leaves less than 32 pixels per cell.")
        for row in range(rows):
            for column in range(columns):
                rectangle = (round(column*size[0]/columns),round(row*size[1]/rows),
                             round((column+1)*size[0]/columns),round((row+1)*size[1]/rows))
                image,bounds = _clean_component(sheet.crop(rectangle))
                cells.append({"image":image,"bounds":bounds,"source_rect":list(rectangle),
                              "source_cell":len(cells)})
    for cell in cells:
        image = cell["image"]
        left,top,right,bottom = opaque_bounds(image)
        if left <= 0 or top <= 0 or right >= image.width or bottom >= image.height:
            raise ValueError(f"Cell {cell['source_cell']+1} has visible artwork on its edge; "
                             "use a transparent, unclipped sheet with padding.")
        if image.getchannel("A").getextrema()[0] != 0:
            raise ValueError(f"Cell {cell['source_cell']+1} has no transparent background.")
    return cells,size


def prepare_frames(cells: list[dict], columns: int) -> tuple[list[Image.Image],dict]:
    bounds = [opaque_bounds(cell["image"]) for cell in cells]
    left = min(box[0] for box in bounds)
    right = max(box[2] for box in bounds)
    height = max(box[3]-box[1] for box in bounds)
    scale = min((CELL_SIZE[0]-2*PADDING)/(right-left),(FLOOR-PADDING)/height)
    # Use the same affine scale for all cells, including sheets whose grid
    # boundaries alternate between313/314px. No individual silhouette sizing.
    scaled_size = (math.ceil(max(cell["image"].width for cell in cells)*scale)+2,
                   math.ceil(max(cell["image"].height for cell in cells)*scale)+2)
    scaled = [cell["image"].transform(scaled_size,Image.Transform.AFFINE,
              (1/scale,0,0,0,1/scale,0),resample=Image.Resampling.BICUBIC) for cell in cells]
    scaled_bounds = [opaque_bounds(image) for image in scaled]
    # Some sheets offset an entire column of animals. Estimate that layout
    # drift across rows from the right-facing muzzle, preserving head movement
    # between drawings instead of pinning each nose independently.
    noses = [statistics.median(box[2] for box in scaled_bounds[col::columns])
             for col in range(columns)]
    column_shift = [0] * columns
    if len(cells) >= 2 * columns and max(noses) - min(noses) > 5:
        center = statistics.median(noses)
        column_shift = [round(center - nose) for nose in noses]
    left = min(box[0] + column_shift[i % columns] for i, box in enumerate(scaled_bounds))
    right = max(box[2] + column_shift[i % columns] for i, box in enumerate(scaled_bounds))
    shared_x = (CELL_SIZE[0] - (right - left)) // 2 - left
    frames,metadata = [],[]
    for cell,source_box,image,box in zip(cells,bounds,scaled,scaled_bounds):
        ground = box[3]-1
        position = shared_x + column_shift[cell["source_cell"] % columns], FLOOR-ground
        frame = Image.new("RGBA",CELL_SIZE)
        frame.alpha_composite(image,position)
        actual = opaque_bounds(frame)
        if actual[3]-1 != FLOOR:
            raise ValueError(f"Cell {cell['source_cell']+1} did not register on the floor.")
        if actual[0] <= 0 or actual[1] <= 0 or actual[2] >= CELL_SIZE[0]:
            raise ValueError(f"Cell {cell['source_cell']+1} would clip visible artwork.")
        frames.append(frame)
        metadata.append({"source_cell":cell["source_cell"],"source_rect":cell["source_rect"],
                         "source_bounds":list(source_box),"source_ground_y":source_box[3]-1,
                         "canvas_position":list(position),"canvas_ground_y":actual[3]-1,
                         "transparent_background":frame.getchannel("A").getextrema()[0] == 0})
    return frames,{"registration":"shared-scale/column-registered-x/actual-lowest-visible-paw-y",
                   "scale":scale,"column_shift":column_shift,"shared_canvas_x":shared_x,"floor_y":FLOOR,
                   "alpha_threshold":ALPHA_THRESHOLD,"frames":metadata}


def frame_rect(entry) -> tuple[int,int,int,int]:
    rectangle = entry.get("rect") if isinstance(entry,dict) else entry
    if (not isinstance(rectangle,list) or len(rectangle) != 4
            or any(not isinstance(value,int) for value in rectangle)
            or tuple(rectangle[2:]) != CELL_SIZE or min(rectangle[:2]) < 0):
        raise ValueError(f"Invalid runtime frame rectangle: {rectangle!r}")
    return tuple(rectangle)


def contact_sheet(frames: list[Image.Image]) -> Image.Image:
    columns = min(4,len(frames))
    width,height,header = 240,192,20
    result = Image.new("RGB",(columns*width,math.ceil(len(frames)/columns)*(height+header)),"#faf2e7")
    draw = ImageDraw.Draw(result)
    for index,frame in enumerate(frames):
        x,y = index%columns*width,index//columns*(height+header)
        draw.text((x+6,y+4),f"WALK {index+1:02}",fill="#70533f")
        sprite = frame.resize((width,height),Image.Resampling.LANCZOS)
        result.paste(sprite,(x,y+header),sprite)
        baseline = y+header+FLOOR*height/CELL_SIZE[1]
        draw.line((x,baseline,x+width-1,baseline),fill="#79ac8a",width=1)
    return result


def import_study(animal: str, sheet: Path, output_root: Path, columns=4,rows=4,dry_run=False,subdivisions=1) -> dict:
    slug = animal.lower()
    source_root = ROOT/"artwork"/"motion"
    manifest_path = source_root/f"{slug}.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    if original.get("format_version") != 1 or original.get("cell_size") != list(CELL_SIZE):
        raise ValueError("The runtime atlas must use format 1 with 400×320 cells.")
    filename = original.get("image")
    if not isinstance(filename,str) or Path(filename).name != filename:
        raise ValueError("Invalid runtime atlas image filename.")
    with Image.open(source_root/filename) as source:
        before = source.convert("RGBA")
    rectangles = [frame_rect(entry) for entry in original["frames"]]
    for x,y,width,height in rectangles:
        if x+width > before.width or y+height > before.height:
            raise ValueError("An existing frame extends outside the runtime atlas.")
    if subdivisions < 1:
        raise ValueError("The number of samples per drawing must be positive.")
    clip = "drawn_walk" if subdivisions > 1 else "walk"
    walk = original["clips"].get(clip, [])
    if (not isinstance(walk,list) or len(set(walk)) != len(walk)
            or any(not isinstance(index,int) or not 0 <= index < len(rectangles) for index in walk)):
        raise ValueError("The runtime walk clip must contain distinct valid frame indexes.")
    other = {name:indexes for name,indexes in original["clips"].items() if name != clip}
    other_references = set(index for indexes in other.values() for index in indexes)
    if other_references.intersection(walk):
        raise ValueError("Walk frames are shared by another pose; refusing to alter that pose.")
    preserved = set(range(len(rectangles)))-set(walk)
    for index in walk:
        x,y,width,height = rectangles[index]
        for keep in preserved:
            kx,ky,kw,kh = rectangles[keep]
            if x < kx+kw and kx < x+width and y < ky+kh and ky < y+height:
                raise ValueError("A walk rectangle overlaps another pose; refusing to alter it.")
    cells,size = load_cells(sheet,columns,rows)
    frames,registration = prepare_frames(cells, columns)
    drawings = frames
    if subdivisions > 1:
        from tools.motion_inbetweens import cyclic_frames
        frames, registration["key_positions"] = cyclic_frames(drawings, subdivisions)
        for index, frame in enumerate(frames):
            # At a contact exchange, flow can interpolate a raised paw an extra
            # pixel below the stationary paw. Keep the floor consistent.
            correction = FLOOR - (opaque_bounds(frame)[3]-1)
            if correction:
                grounded = Image.new("RGBA", CELL_SIZE)
                grounded.alpha_composite(frame, (0, correction))
                frames[index] = grounded
        registration["subdivisions"] = subdivisions
        registration["interpolation"] = "bidirectional optical flow"
    manifest = copy.deepcopy(original)
    indexes = walk[:len(frames)]
    # Extra drawings get new slots below the original image. Existing frame
    # positions/indexes remain unchanged, even when the new grid has more poses.
    extra = max(0,len(frames)-len(walk))
    atlas_columns = max(1,before.width//CELL_SIZE[0])
    extra_rows = math.ceil(extra/atlas_columns)
    atlas = Image.new("RGBA",(before.width,before.height+extra_rows*CELL_SIZE[1]))
    atlas.paste(before,(0,0))
    for offset in range(extra):
        indexes.append(len(manifest["frames"]))
        manifest["frames"].append([offset%atlas_columns*CELL_SIZE[0],
            before.height+offset//atlas_columns*CELL_SIZE[1],*CELL_SIZE])
    for index,frame in zip(indexes,frames):
        x,y,_width,_height = frame_rect(manifest["frames"][index])
        atlas.paste(frame,(x,y))  # Replace transparent pixels too; never overlay old paws.
    for index in preserved:
        x,y,width,height = rectangles[index]
        rectangle = x,y,x+width,y+height
        if before.crop(rectangle).tobytes() != atlas.crop(rectangle).tobytes():
            raise RuntimeError(f"Preserved frame {index} unexpectedly changed.")
    manifest["clips"][clip] = indexes
    manifest.setdefault("registration",{})[clip] = registration
    try:
        source_name = str(sheet.resolve().relative_to(ROOT))
    except ValueError:
        source_name = sheet.name
    source_entry = {
        "file":source_name,"size":list(size),"grid":[columns,rows],
        "sha256":hashlib.sha256(sheet.read_bytes()).hexdigest(),
    }
    sources = manifest.setdefault("sources",{})
    source_key = next((key for key,value in sources.items()
                       if key.startswith("walk_study") and value == source_entry),None)
    if source_key is None:
        source_key,version = "walk_study",1
        while source_key in sources:
            version += 1
            source_key = f"walk_study_{version}"
        sources[source_key] = source_entry
    registration["source_key"] = source_key
    output_image = output_root/filename
    output_manifest = output_root/manifest_path.name
    preview = ROOT/"build"/"walk-study"/f"{slug}-contact.png"
    report = {"animal":animal.title(),"dry_run":dry_run,"walk_frames":len(frames),
              "preserved_pose_frames":len(preserved),"preserved_pixels_verified":True,
              "original_sources_retained":all(manifest["sources"][key] == value
                  for key,value in original.get("sources",{}).items()),
              "source_key":source_key,"shared_scale":registration["scale"],"shared_x":registration["shared_canvas_x"],
              "floor_y":FLOOR,"alpha_threshold":ALPHA_THRESHOLD,
              "transparent_frames":sum(item["transparent_background"] for item in registration["frames"]),
              "atlas":str(output_image),"manifest":str(output_manifest),"preview":str(preview)}
    if not report["original_sources_retained"]:
        raise RuntimeError("An original source metadata entry unexpectedly changed.")
    if not dry_run:
        output_root.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output_root,suffix=".png",delete=False) as handle:
            image_temp = Path(handle.name)
            atlas.save(handle,format="PNG")
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=output_root,
                                         suffix=".json",delete=False) as handle:
            manifest_temp = Path(handle.name)
            json.dump(manifest,handle,indent=2)
            handle.write("\n")
        image_temp.replace(output_image)
        manifest_temp.replace(output_manifest)
        preview.parent.mkdir(parents=True,exist_ok=True)
        contact_sheet(drawings).save(preview)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--animal",required=True,choices=("cat","dog","fox"))
    parser.add_argument("--sheet",required=True,type=Path)
    parser.add_argument("--subdivisions", type=int, default=1,
                        help="Bake this many samples per drawing on average, including the loop seam.")
    parser.add_argument("--columns",type=int,default=4)
    parser.add_argument("--rows",type=int,default=4)
    parser.add_argument("--output-root",type=Path,default=ROOT/"artwork"/"motion")
    parser.add_argument("--dry-run",action="store_true",help="Validate and report without writing any files.")
    args = parser.parse_args()
    try:
        report = import_study(args.animal,args.sheet,args.output_root,args.columns,args.rows,args.dry_run,args.subdivisions)
    except (OSError,KeyError,ValueError) as exc:
        parser.exit(1,f"Walk import failed: {exc}\n")
    print(json.dumps(report,indent=2))


if __name__ == "__main__":
    main()
