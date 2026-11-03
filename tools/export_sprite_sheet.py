#!/usr/bin/env python3
"""Export the app's natural animal artwork without opening the desktop pet.

    .venv/bin/python -B tools/export_sprite_sheet.py

The presentation sheet has a solid background. Supplied Cat, Dog and Fox artwork
is exported directly at 400 x 320; Crow retains its native 60 x 48 pixel grid.
"""
from __future__ import annotations

import argparse
import json
import math
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


def render_frame(animal, pose, frame, **motion):
    if animal in ("Cat","Dog","Fox"):
        image = pet.animal_frame_image(animal,pose,frame,
                                       palette=pet.PALETTES["Natural Coat"],**motion)
    else:
        image = pet.DRAWERS[animal](pose,frame,1,
            palette=pet.PALETTES["Natural Coat"],**motion).toImage().scaled(
                *CELL,Qt.AspectRatioMode.IgnoreAspectRatio,Qt.TransformationMode.FastTransformation)
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA",(image.width(),image.height()),
        image.constBits().asstring(image.sizeInBytes()),"raw","RGBA",image.bytesPerLine()).copy()


def specifications():
    rows = [("Crow",[("rest",0,"PERCH",600)]+[("fly",i,f"FLIGHT {i+1:02}",55) for i in range(8)])]
    for animal in ("Cat","Dog","Fox"):
        count = 4 if animal == "Dog" else 6
        frames = [("rest",0,"REST",600)]
        frames += [("walk",i,f"WALK {i+1:02}",90) for i in range(count)]
        frames += [("petted",0,"PETTED",90),("sleep",0,"SLEEP",800)]
        if animal == "Dog":
            frames += [("groom",0,"GROOM",180),("watch",0,"WATCH",180)]
        rows.append((animal,frames))
    return rows


def animation_specifications():
    rows = specifications()[:1]
    for animal in ("Cat","Dog","Fox"):
        poses = (("rest",1,600),("blink",1,140),("walk",4 if animal=="Dog" else 6,90),
                 ("sleep",2,800),("held",4,65),("fall",4,65),("petted",4,90),
                 ("land",3,60),("groom",4,180),("yawn",4,180),("stretch",4,180),("watch",4,180))
        rows.append((animal,[(pose,i,pose.upper(),duration) for pose,count,duration in poses
                             for i in range(count)]))
    return rows


def export_animal_preview(output,animal):
    sequence = []
    for i in range(12):
        phase = math.sin(i*math.tau/12)
        sequence.append(("rest",0,"IDLE",150,dict(tail_offset=round(phase*2),breath=-int(phase>0.4))))
    count = 4 if animal=="Dog" else 6
    sequence += [("walk",i,"WALK",90,{}) for _ in range(2) for i in range(count)]
    sequence += [("petted",i,"PETTED",90,{}) for _ in range(2) for i in range(4)]
    sequence += [("fall",i,"AIRBORNE",65,dict(stretch=(1,2,3,2)[i])) for i in range(4)]
    sequence += [("land",i,"LANDING",60,{}) for i in range(3)]
    sequence += [("sleep",i,"SLEEP",650,{}) for i in range(2)]
    native = render_frame(animal,"rest",0).size
    images,durations = [],[]
    for pose,i,label,duration,motion in sequence:
        canvas = Image.new("RGB",(native[0]+64,native[1]+72),"#faf2e7")
        bitmap_text(canvas,f"{animal.upper()} - {label}",(24,14),2,"#70533f")
        sprite = render_frame(animal,pose,i,**motion)
        canvas.paste(sprite,(32,48),sprite)
        images.append(canvas)
        durations.append(duration)
    path = output/f"{animal.lower()}-animations.gif"
    images[0].save(path,save_all=True,append_images=images[1:],duration=durations,
                   loop=0,disposal=2,optimize=False)
    return path


def export(output,scale=3):
    output.mkdir(parents=True,exist_ok=True)
    rows,full = specifications(),animation_specifications()
    frames = {(animal,pose,i):render_frame(animal,pose,i)
              for animal,sequence in full for pose,i,_,_ in sequence}
    manifest = dict(format_version=2,direction="right",coat="natural",animals={})
    paths = {}
    for animal,sequence in full:
        cell = frames[animal,"rest",0].size
        columns = 6
        atlas = Image.new("RGBA",(cell[0]*columns,cell[1]*math.ceil(len(sequence)/columns)))
        clips = {}
        name = animal.lower()
        filename = f"{name}-sprites-atlas.png"
        for index,(pose,frame,_,duration) in enumerate(sequence):
            x,y = (index%columns)*cell[0],(index//columns)*cell[1]
            atlas.alpha_composite(frames[animal,pose,frame],(x,y))
            clips.setdefault(pose,[]).append(dict(frame=frame,rect=[x,y,*cell],duration_ms=duration))
        atlas.save(output/filename)
        item = dict(image=filename,cell_size=list(cell),columns=columns,clips=clips)
        manifest["animals"][name] = item
        (output/f"{name}-sprites-atlas.json").write_text(json.dumps(
            dict(format_version=2,animal=name,**item),indent=2)+"\n",encoding="utf-8")
        paths[f"{name}_atlas"] = str(output/filename)
        if animal=="Crow":
            atlas.save(output/"animal-sprites-atlas.png")
    (output/"animal-sprites-atlas.json").write_text(json.dumps(manifest,indent=2)+"\n",encoding="utf-8")
    tile = (60*scale,48*scale)
    margin,gap,header,caption = 24,12,28,26
    row_height = tile[1]+header+caption+24
    sheet = Image.new("RGB",(margin*2+(tile[0]+gap)*9-gap,52+row_height*len(rows)),BACKGROUND)
    bitmap_text(sheet,"LIL BUDDY CHARACTER ANIMATIONS",(margin,17),2)
    for row,(animal,sequence) in enumerate(rows):
        top = 52+row*row_height
        bitmap_text(sheet,animal,(margin,top),2)
        for column,(pose,i,label,_) in enumerate(sequence):
            x = margin+column*(tile[0]+gap)
            sampling = Image.Resampling.NEAREST if animal=="Crow" else Image.Resampling.LANCZOS
            sprite = frames[animal,pose,i].resize(tile,sampling)
            sheet.paste(sprite,(x,top+header),sprite)
            bitmap_text(sheet,label,(x,top+header+tile[1]+8),1,MUTED)
    sheet.save(output/"animal-sprite-sheet.png")
    paths["sheet"] = str(output/"animal-sprite-sheet.png")
    # A clear full-detail comparison of the three supplied character models.
    native = pet.SPRITE_CANVAS
    comparison = Image.new("RGB",(native[0]*3,native[1]+44),"#faf2e7")
    for col,animal in enumerate(("Cat","Dog","Fox")):
        bitmap_text(comparison,animal,(col*native[0]+24,14),2,"#70533f")
        sprite = frames[animal,"rest",0]
        comparison.paste(sprite,(col*native[0],44),sprite)
        paths[f"{animal.lower()}_preview"] = str(export_animal_preview(output,animal))
    comparison.save(output/"character-models-preview.png")
    paths["models"] = str(output/"character-models-preview.png")
    strip = Image.new("RGBA",(CELL[0]*8,CELL[1]))
    crow_frames = []
    for i in range(8):
        sprite = frames["Crow","fly",i]
        strip.alpha_composite(sprite,(i*CELL[0],0))
        canvas = Image.new("RGB",(380,288),BACKGROUND)
        enlarged = sprite.resize((300,240),Image.Resampling.NEAREST)
        canvas.paste(enlarged,(40,24),enlarged)
        crow_frames.append(canvas)
    strip.save(output/"crow-flight-strip.png")
    crow_frames[0].save(output/"crow-flight.gif",save_all=True,append_images=crow_frames[1:],
                        duration=[50,60]*4,loop=0,disposal=2,optimize=False)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/"artwork")
    parser.add_argument("--scale",type=int,choices=(2,3,4),default=3)
    options = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    paths = export(options.output.resolve(),options.scale)
    app.processEvents()
    print(json.dumps(paths,indent=2))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
