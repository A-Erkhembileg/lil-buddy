#!/usr/bin/env python3
"""Export the actual sprite renderer as a compact 60 fps MP4 (optional FFmpeg).

    python tools/render_motion_video.py --ffmpeg /path/to/ffmpeg

Alternatively install imageio-ffmpeg for this development tool. The desktop app
does not import or bundle FFmpeg, Pillow, or this exporter.
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
from PIL import Image
from PyQt6.QtWidgets import QApplication
from tools.export_sprite_sheet import bitmap_text,render_frame


def export(destination,encoder):
    animals=("Cat","Dog","Fox")
    width,height=1200,392
    stages=(("rest","IDLE",1.20,2.40,4),("walk","16 POSE WALK",2.56,.64,16),
            ("petted","PETTED",1.44,.48,6),("fall","AIRBORNE",.90,.30,6),
            ("land","LANDING",.20,.18,3),("sleep","SLEEP",1.20,3.60,2))
    destination.parent.mkdir(parents=True,exist_ok=True)
    command=[encoder,"-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24",
             "-s",f"{width}x{height}","-r","60","-i","-","-an","-c:v","libx264",
             "-preset","fast","-crf","18","-pix_fmt","yuv420p","-movflags","+faststart",
             str(destination)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE)
    count=0
    try:
        for pose,label,seconds,cycle,frames in stages:
            for tick in range(round(seconds*60)):
                elapsed=tick/60
                phase=(elapsed/cycle)%1.0
                frame=min(frames-1,int(phase*frames))
                if pose=="land":frame=min(2,int(elapsed/.06))
                canvas=Image.new("RGB",(width,height),"#faf2e7")
                for column,animal in enumerate(animals):
                    bitmap_text(canvas,f"{animal.upper()} - {label}",(column*400+16,16),2,"#70533f")
                    motion={"motion_phase":None if pose=="land" else phase}
                    if pose=="fall":motion["stretch"]=2
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg")
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
    result=export(options.output.resolve(),encoder)
    application.processEvents()
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
