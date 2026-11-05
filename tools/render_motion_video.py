#!/usr/bin/env python3
"""Export the actual sprite renderer as a compact 60 fps MP4 (optional FFmpeg).

    python tools/render_motion_video.py --ffmpeg /path/to/ffmpeg

Alternatively install imageio-ffmpeg for this development tool. The desktop app
does not import or bundle FFmpeg or this exporter.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication
from tools.export_sprite_sheet import bitmap_text,preview_timeline,render_frame
import desktop_cat as pet


def export(destination,encoder):
    animals=("Cat","Dog","Fox")
    width,height=1200,392
    destination.parent.mkdir(parents=True,exist_ok=True)
    command=[encoder,"-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24",
             "-s",f"{width}x{height}","-r","60","-i","-","-an","-c:v","libx264",
             "-preset","fast","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",
             str(destination)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE)
    count=0
    try:
        timelines = [preview_timeline(animal,60,full=True) for animal in animals]
        for poses in zip(*timelines, strict=True):
            canvas=Image.new("RGB",(width,height),"#faf2e7")
            for column,(animal,(pose,frame,label,motion)) in enumerate(zip(animals,poses)):
                bitmap_text(canvas,f"{animal.upper()} - {label}",(column*400+16,16),2,"#70533f")
                sprite=render_frame(animal,pose,frame,**motion)
                canvas.paste(sprite,(column*400,48),sprite)
            process.stdin.write(canvas.tobytes())
            count+=1
        process.stdin.close()
        if process.wait()!=0:raise RuntimeError("The video encoder failed.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return dict(path=str(destination),fps=60,frames=count,seconds=count/60,bytes=destination.stat().st_size)


def export_walk_check(destination,encoder):
    """Move pets over a fixed floor; the fixed ruler makes paw travel visible."""
    width,height,scale = 1000,744,0.6
    destination.parent.mkdir(parents=True,exist_ok=True)
    command=[encoder,"-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24",
             "-s",f"{width}x{height}","-r","60","-i","-","-an","-c:v","libx264",
             "-preset","fast","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",
             str(destination)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE)
    frames=360
    try:
        for tick in range(frames):
            canvas=Image.new("RGB",(width,height),"#faf2e7")
            draw=ImageDraw.Draw(canvas)
            elapsed=tick/60
            for row,animal in enumerate(("Cat","Dog","Fox")):
                base=row*248
                floor=base+236
                rig=pet.WALK_RIGS[animal]
                travel=rig["speed"]*scale/(pet.WIDTH/400)*elapsed
                phase=travel/(rig["stride"]/pet.WALK_DUTY*scale) % 1.0
                x=30+travel
                bitmap_text(canvas,f"{animal.upper()} - CONTINUOUS WALK",(18,base+10),2,"#70533f")
                draw.line((12,floor,988,floor),fill="#bbae9b",width=1)
                for marker in range(20,990,40):
                    draw.line((marker,floor,marker,floor+6),fill="#bbae9b",width=1)
                sprite=render_frame(animal,"walk",0,motion_phase=phase).resize((240,192),Image.Resampling.LANCZOS)
                canvas.paste(sprite,(round(x),base+45),sprite)
            process.stdin.write(canvas.tobytes())
        process.stdin.close()
        if process.wait()!=0:raise RuntimeError("The walk preview encoder failed.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return dict(path=str(destination),fps=60,frames=frames,seconds=frames/60,bytes=destination.stat().st_size)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--walk-only",action="store_true",help="Show walking against a fixed floor ruler.")
    parser.add_argument("--output",type=Path,default=ROOT/"artwork"/"fluid-motion-60fps.mp4")
    options=parser.parse_args()
    encoder=options.ffmpeg or shutil.which("ffmpeg")
    if not encoder:
        try:
            import imageio_ffmpeg
            encoder=imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            parser.error("Install optional imageio-ffmpeg or provide --ffmpeg.")
    application=QApplication.instance() or QApplication([])
    renderer=export_walk_check if options.walk_only else export
    result=renderer(options.output.resolve(),encoder)
    application.processEvents()
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
