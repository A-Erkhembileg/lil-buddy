#!/usr/bin/env python3
"""Render the live app artwork and pair it with the supplied motion study.

The source study is never replaced. A matched 13-chapter study and a separate
continuous / slow-motion mechanics audit accompany the side-by-side comparison.
All poses come from desktop_cat; this file supplies only the stage and clock.
"""
from __future__ import annotations

import argparse
import inspect
import hashlib
import json
import math
import os
import random
import time
from pathlib import Path
import shutil
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw, ImageFont
from PyQt6.QtCore import Qt, QPointF, QRectF, QEvent
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication
import desktop_cat as pet

FPS = 60
SIZE = (1280, 800)
ANIMALS = ("Cat", "Dog", "Fox", "Crow")
BACKGROUND = "#f5f2e9"
INK = "#25382e"
MUTED = "#68756a"
CHAPTERS = (
    ("Resting", 3.6, "Quiet breathing and offset secondary movement"),
    ("Blinking", 3.0, "Independent eyelids, stable face and markings"),
    ("Walking", 7.6, "Distance-driven gait against fixed floor marks"),
    ("Grooming", 3.5, "Paw lift, contact, licking, pause and lowering"),
    ("Yawning", 2.5, "Preparation, hinged jaw, peak and relaxed closure"),
    ("Stretching", 3.1, "Planted front paws, lowered chest and raised hips"),
    ("Watching", 3.0, "Quiet holds with a restrained head response"),
    ("Petting", 3.3, "A short response followed by a quiet recovery"),
    ("Sleeping", 4.2, "Lowering into sleep and a settled breathing hold"),
    ("Picking up", 3.0, "Supported torso, gravity-led limbs and delayed tail"),
    ("Falling and landing", 2.3, "Contact preparation, compression, recovery, stance"),
    ("Corner peek", 2.0, "Existing character artwork retained"),
    ("Crow flight", 9.0, "Takeoff, asymmetric wingbeats, steering and landing"),
)


def font(size, bold=False):
    paths = [f"/System/Library/Fonts/Supplemental/Arial{' Bold' if bold else ''}.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for path in paths:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


FONTS = {size: font(size, size in (16, 30)) for size in (13, 14, 16, 18, 30)}


def smooth(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def pil_image(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombytes("RGBA", (image.width(), image.height()),
                           image.constBits().asstring(image.sizeInBytes()),
                           "raw", "RGBA", image.bytesPerLine()).copy()


def sprite(animal, pose, phase, direction=1, **kwargs):
    if animal != "Crow":
        return pil_image(pet.animal_frame_image(animal, pose, 0, direction,
            palette=pet.PALETTES["Natural Coat"], motion_phase=phase, **kwargs))
    drawer = pet.draw_crow
    extra = {key: value for key, value in dict(kwargs, motion_phase=phase).items()
             if key in inspect.signature(drawer).parameters}
    count = 8 if pose in ("fly", "held", "fall") else 4
    # Pixel artwork remains on its native grid; enlarge with nearest sampling.
    frame = int(phase * count) % count
    if pose == "land":
        frame = min(2, int(phase * 3))
    return pil_image(drawer(pose, frame, direction,
        palette=pet.PALETTES["Natural Coat"], **extra).toImage()).resize((60, 48), Image.Resampling.NEAREST)


def encoder_path(explicit=None):
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    bundled = ROOT / "build/preview-tools/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"
    if found:
        return found
    if bundled.is_file():
        return str(bundled)
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class Video:
    def __init__(self, destination, encoder, size=SIZE):
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination, self.frames = destination, 0
        self.process = subprocess.Popen([encoder, "-y", "-loglevel", "error", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(FPS), "-i", "-",
            "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(destination)], stdin=subprocess.PIPE)

    def write(self, image):
        self.process.stdin.write(image.convert("RGB").tobytes())
        self.frames += 1

    def finish(self):
        self.process.stdin.close()
        if self.process.wait():
            raise RuntimeError(f"Video encoding failed: {self.destination}")
        return dict(path=str(self.destination), fps=FPS, frames=self.frames,
                    seconds=self.frames/FPS, bytes=self.destination.stat().st_size)


def stage(title, subtitle, chapter, elapsed, total, single=False):
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((28, 18), "LIL BUDDY  /  UPDATED APP RENDERER", font=FONTS[14], fill=MUTED)
    draw.text((28, 48), title, font=FONTS[30], fill=INK)
    draw.text((590, 57), subtitle, font=FONTS[16], fill=MUTED)
    draw.text((1160, 20), chapter, font=FONTS[14], fill=MUTED)
    draw.text((28, 760), "NATURAL COATS  /  FIXED FLOOR MARKS", font=FONTS[13], fill=MUTED)
    draw.text((1030, 760), f"{elapsed:05.2f} s  /  60 fps", font=FONTS[13], fill=MUTED)
    draw.line((28, 787, 1252, 787), fill="#d9ded4", width=2)
    draw.line((28, 787, 28 + round(1224*elapsed/max(total, 1)), 787), fill="#64836b", width=2)
    cards = ((28, 108, 1224, 626),) if single else ((28,108,600,300),(652,108,600,300),
                                                                  (28,430,600,300),(652,430,600,300))
    for index, (x,y,w,h) in enumerate(cards):
        draw.rounded_rectangle((x,y,x+w,y+h), 12, fill="#fffdf9")
        animal = "Crow" if single else ANIMALS[index]
        draw.text((x+14,y+12), "HUSKY" if animal == "Dog" else animal.upper(), font=FONTS[16], fill=INK)
        floor = y+h-18
        draw.line((x+10,floor,x+w-10,floor),fill="#a4ac9e",width=1)
        for marker in range(x+20,x+w-10,40):
            draw.line((marker,floor,marker,floor+5),fill="#b7bfb0",width=1)
    return image, cards


def put(image, animal, drawing, card, x_offset=0, height=0, label=None, scale=None):
    x,y,w,h = card
    scale = scale if scale is not None else (3 if animal == "Crow" else .6)
    source_height=drawing.height
    pose=(label or "").split(" /")[0].lower()
    floor_anchor=(pet.WALK_FLOOR if animal != "Crow" and pose in
                  ("walk","held","fall","land","turn") else source_height)
    drawing = drawing.resize((round(drawing.width*scale),round(drawing.height*scale)),
                             Image.Resampling.NEAREST if animal == "Crow" else Image.Resampling.LANCZOS)
    px = round(x+(w-drawing.width)/2+x_offset)
    py = round(y+h-18-floor_anchor*scale-height)
    image.paste(drawing,(px,py),drawing)
    if label:
        ImageDraw.Draw(image).text((x+w-175,y+12),label,font=FONTS[13],fill=MUTED)


def sample(animal, chapter, t, duration):
    """Return pose, progress and stage translation; chapter cuts are intentional."""
    if chapter == "Resting": return "rest",t/3.0,0,0,1,{}
    if chapter == "Blinking":
        local = t%1.3
        return ("blink",local/.18,0,0,1,{}) if local < .18 else ("rest",t/3,0,0,1,{})
    if chapter == "Walking":
        rig = pet.WALK_RIGS.get(animal)
        speed = rig["speed"]*.6/(pet.WIDTH/400) if rig else 40
        stride = rig["stride"]/pet.WALK_DUTY*.6 if rig else 26
        turn = pet.Motion().turn_duration
        half = (duration-turn)/2
        if t < half:
            travel=t*speed
            return "walk",travel/stride,-120+travel,0,1,{}
        if t < half+turn:
            return "turn",(t-half)/turn,-120+half*speed,0,1 if t < half+turn/2 else -1,{}
        travel=(t-half-turn)*speed
        return "walk",travel/stride,-120+half*speed-travel,0,-1,{}
    if chapter in ("Grooming","Yawning","Stretching"):
        pose = {"Grooming":"groom","Yawning":"yawn","Stretching":"stretch"}[chapter]
        gesture = pet.HABIT_DURATIONS[pose]
        start=0 if pose == "stretch" else .15
        if t < start: return "rest",0,0,0,1,{}
        if t < start+gesture: return pose,min(1,(t-start)/gesture),0,0,1,{}
        return "rest",0,0,0,1,{}
    if chapter == "Watching": return "watch",t/duration,0,0,1,{"dangle":0}
    if chapter == "Petting": return "petted",min(1,t/1.28),0,0,1,{}
    if chapter == "Sleeping":
        transition=pet.Motion().transition_duration
        if t < transition: return "settle",t/transition,0,0,1,{}
        return "sleep",(t-transition)/pet.ASSET_ANIMATION_SECONDS["sleep"],0,0,1,{}
    if chapter == "Picking up":
        return "held",min(1,t/.8),0,96*smooth(t/.9),1,{"dangle":0,"stretch":0}
    if chapter == "Falling and landing":
        fall=.52
        landing = pet.Motion().landing_duration
        if t < fall: return "fall",t/fall,0,96*(1-(t/fall)**2),1,{"stretch":min(3,t/fall*2)}
        if t < fall+landing: return "land",(t-fall)/landing,0,0,1,{}
        sit=pet.Motion().transition_duration
        if t < fall+landing+sit: return "sit",(t-fall-landing)/sit,0,0,1,{}
        return "rest",0,0,0,1,{}
    if chapter == "Corner peek": return "peek",0,0,0,1,{}
    raise ValueError(chapter)


def peek(animal):
    return pil_image(pet.draw_peek(animal,pet.PALETTES["Natural Coat"]).toImage())


def render_study(destination, encoder, snapshots):
    video = Video(destination,encoder)
    elapsed=0.0
    total=sum(item[1] for item in CHAPTERS)
    for index,(title,duration,subtitle) in enumerate(CHAPTERS):
        if title == "Crow flight":
            flight_area=QRectF(0,0,1000,500)
            flight=pet.Motion(x=30,y=404,roaming=False)
            flight_rng=random.Random(4)
            flight_floor=flight.bounds(flight_area)[3]
        for tick in range(round(duration*FPS)):
            t=tick/FPS
            single=title=="Crow flight"
            image,cards=stage(title,subtitle,f"{index+1:02} / 13",elapsed+t,total,single)
            if single:
                if tick == 24:
                    flight.begin_flight(flight_area,flight_rng)
                if tick >= 24:
                    for _ in range(2): flight.step(1/120,flight_area,flight_rng,autonomous=False)
                pose=flight.state
                phase=(flight.flight_time/(8*pet.CROW_FLAP_SECONDS) if pose=="fly" else
                       flight.transition_time/flight.landing_duration if pose=="land" else
                       flight.transition_time/flight.transition_duration if pose=="sit" else t/3)
                x=(flight.x-30)*1.5-380
                above=(flight_floor-flight.y)*1.5
                pitch=max(-1,min(1,round(flight.vy/80)))
                put(image,"Crow",sprite("Crow",pose,phase,flight.direction,dangle=pitch),cards[0],x,above,pose.upper(),scale=5)
            else:
                for animal,card in zip(ANIMALS,cards):
                    pose,phase,x,above,direction,options=sample(animal,title,t,duration)
                    drawing = peek(animal) if pose == "peek" else sprite(animal,pose,phase,direction,**options)
                    put(image,animal,drawing,card,x,above,pose.upper())
            video.write(image)
            if tick in {round(duration*FPS*.3),round(duration*FPS*.6)}:
                image.save(snapshots/f"{index+1:02}-{title.lower().replace(' ','-')}-{tick:03}.jpg")
        elapsed+=duration
        print(f"Rendered {index+1}/13: {title}",flush=True)
    return video.finish()


def metadata(destination,encoder):
    path=destination.with_suffix(".chapters.txt")
    lines=[";FFMETADATA1"]
    frames=0
    for title,seconds,_ in CHAPTERS:
        end=frames+round(seconds*FPS)
        lines.extend(("[CHAPTER]","TIMEBASE=1/60",f"START={frames}",f"END={end}",f"title={title}"))
        frames=end
    path.write_text("\n".join(lines)+"\n")
    temp=destination.with_name(destination.stem+"-tagged.mp4")
    subprocess.run([encoder,"-y","-loglevel","error","-i",str(destination),"-i",str(path),
                    "-map_metadata","1","-codec","copy",str(temp)],check=True)
    temp.replace(destination)


def compare(before,after,destination,encoder,work):
    header=Image.new("RGB",(2560,56),BACKGROUND)
    draw=ImageDraw.Draw(header)
    draw.text((28,14),"BEFORE  /  SUPPLIED REFERENCE",font=FONTS[18],fill=INK)
    draw.text((1308,14),"AFTER  /  UPDATED IMPLEMENTATION",font=FONTS[18],fill=INK)
    banner=work/"comparison-header.png"
    header.save(banner)
    subprocess.run([encoder,"-y","-loglevel","error","-i",str(before),"-i",str(after),
        "-loop","1","-i",str(banner),"-filter_complex",
        "[0:v][1:v]hstack=inputs=2[body];[2:v][body]vstack=inputs=2[v]",
        "-map","[v]","-an","-r","60","-c:v","libx264","-preset","fast","-crf","18",
        "-pix_fmt","yuv420p","-t",str(sum(c[1] for c in CHAPTERS)),"-movflags","+faststart",str(destination)],check=True)
    metadata(destination,encoder)


def slow_motion(comparison,destination,encoder,work):
    # Normal-speed comparison is followed separately by quarter-speed inspection
    # of the complete release/landing and wing cycle; no timing is hidden.
    header=Image.new("RGB",(2560,48),BACKGROUND)
    ImageDraw.Draw(header).text((28,14),"QUARTER SPEED / 0.25x / SOURCE TIMECODE SHOWN",font=FONTS[18],fill=INK)
    banner=work/"slow-motion-header.png"
    header.save(banner)
    subprocess.run([encoder,"-y","-loglevel","error","-i",str(comparison),"-loop","1","-i",str(banner),"-filter_complex",
        "[0:v]split=3[a][b][c];[a]trim=start=14.2:end=23.3,setpts=4*(PTS-STARTPTS)[gestures];"
        "[b]trim=start=33.8:end=39.1,setpts=4*(PTS-STARTPTS)[landing];"
        "[c]trim=start=41.1:end=44.1,setpts=4*(PTS-STARTPTS)[flight];"
        "[gestures][landing][flight]concat=n=3:v=1:a=0[body];[1:v][body]vstack=inputs=2[v]",
        "-map","[v]","-an","-r","60","-t","69.6","-c:v","libx264","-preset","fast","-crf","18",
        "-pix_fmt","yuv420p","-movflags","+faststart",str(destination)],check=True)



def runtime_audit(destination, encoder, work):
    """Record real _tick state changes, with synthetic mouse input, at 60 Hz."""
    video=Video(destination,encoder)
    telemetry=[]
    area=QRectF(0,0,480,400)
    app=QApplication.instance()
    total=15.0
    elapsed=0.0
    for segment,duration in (("Interrupted grooming, pickup, drop and delayed sleep",8.0),
                             ("Continuous walks, repeated strides and boundary turns",7.0)):
        cats=[]
        for animal in ANIMALS:
            cat=pet.DesktopCat(enable_tray=False)
            cat.timer.stop(); cat.fullscreen_timer.stop()
            cat.available_area=lambda: area
            cat.set_animal_type(animal)
            cat.timer.stop(); cat.fullscreen_timer.stop()
            cat._habits_enabled=False
            cat._cursor_interaction=False
            cat._habit=None
            cat._landing_elapsed=None
            cat._last_area=area
            cat._animation_time=0.0
            cat._walk_phase=0.0
            cat._pose_clock_kind=None
            cat.motion.x=300.0
            cat.motion.y=cat.motion.bounds(area)[3]
            cat.motion.decision_in=999.0
            cat.motion.sleeping=False
            cat.motion.roaming=False
            cat.motion.state="rest"
            cat.rng.seed(17)
            if elapsed == 0:
                cat._habit="groom"
                cat._habit_duration=pet.HABIT_DURATIONS["groom"]
                cat._habit_elapsed=0.0
            else:
                cat.motion.x=cat.motion.bounds(area)[1]-70
                cat.motion.state="walk"
                cat.motion.roaming=True
                cat.motion.direction=1
                cat.motion.decision_in=999
            cat.move(round(cat.motion.x),round(cat.motion.y))
            cat._set_frame()
            cats.append(cat)
        base=time.monotonic()
        for cat in cats: cat._last_time=base
        previous=[None]*4
        for tick in range(round(duration*FPS)):
            t=tick/FPS
            now=base+t
            image,cards=stage("Interruptions and recovery" if elapsed == 0 else "Walks and boundary turns",
                              "ACTUAL APP CLOCK / CONTINUOUS SHOT", "AUDIT", elapsed+t,total)
            for index,(animal,cat,card) in enumerate(zip(ANIMALS,cats,cards)):
                if elapsed == 0:
                    if tick == 60:
                        point=QPointF(cat.motion.x+60,cat.motion.y+45)
                        event=QMouseEvent(QEvent.Type.MouseButtonPress,QPointF(60,45),point,
                            Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
                        cat.mousePressEvent(event)
                        cat._drag_sample_time=now
                    if 60 <= tick < 150:
                        rise=80*smooth((t-1)/.7)
                        point=QPointF(360,cat.motion.bounds(area)[3]+45-rise)
                        cat._drag_to(point,now=now)
                    if tick == 150:
                        point=QPointF(360,cat.motion.y+45)
                        event=QMouseEvent(QEvent.Type.MouseButtonRelease,QPointF(60,45),point,
                            Qt.MouseButton.LeftButton,Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier)
                        cat.mouseReleaseEvent(event)
                        cat._last_time=now-1/FPS
                    if tick == 180:
                        # A click during compression must wait for landing and sit.
                        cat.pet(now=now)
                    if tick == 390:
                        cat._dismiss_reaction()
                        cat.motion.roaming=True
                        sleep_rng=random.Random(1)
                        cat.motion.begin_rest(sleep_rng)
                        cat.motion.decision_in=999
                cat._tick(now=now)
                drawing=pil_image(cat._pixmap.toImage())
                if animal == "Crow":
                    drawing=drawing.resize((60,48),Image.Resampling.NEAREST)
                else:
                    drawing=drawing.resize((400,320),Image.Resampling.LANCZOS)
                floor=cat.motion.bounds(area)[3]
                origin=300 if elapsed == 0 else cat.motion.bounds(area)[1]-70
                put(image,animal,drawing,card,(cat.motion.x-origin)*2,(floor-cat.motion.y)*2,
                    (cat.motion.state+" / "+("SLEEP" if cat.motion.sleeping else "AWAKE")).upper())
                state=(cat.motion.state,cat.motion.sleeping,cat.motion.direction)
                if state != previous[index]:
                    telemetry.append(dict(segment=segment,time=round(t,3),animal=animal,state=state[0],
                                          sleeping=state[1],direction=state[2]))
                    previous[index]=state
            video.write(image)
            if tick in (180,205,235,420): image.save(work/f"audit-{round(elapsed)}-{tick}.jpg")
        for cat in cats:
            cat.close(); cat.deleteLater()
        app.processEvents()
        elapsed+=duration
    (work/"runtime-audit-transitions.json").write_text(json.dumps(telemetry,indent=2)+"\n")
    return video.finish()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--output",type=Path,default=ROOT/"artwork/lil-buddy-motion-before-after.mp4")
    parser.add_argument("--before",type=Path,default=ROOT/"artwork/lil-buddy-motion-showcase.mp4")
    parser.add_argument("--snapshots-only",action="store_true")
    parser.add_argument("--audit-only",action="store_true")
    options=parser.parse_args()
    encoder=encoder_path(options.ffmpeg)
    app=QApplication.instance() or QApplication([])
    work=ROOT/"build/motion-review"
    work.mkdir(parents=True,exist_ok=True)
    snapshots=work/"after-frames"
    snapshots.mkdir(exist_ok=True)
    if options.audit_only:
        print(json.dumps(runtime_audit(options.output,encoder,work),indent=2))
        return
    if options.snapshots_only:
        for index,(title,duration,subtitle) in enumerate(CHAPTERS[:-1]):
            for fraction in (.25,.5,.75):
                image,cards=stage(title,subtitle,f"{index+1:02} / 13",0,1)
                for animal,card in zip(ANIMALS,cards):
                    pose,phase,x,above,direction,kwargs=sample(animal,title,duration*fraction,duration)
                    drawing=peek(animal) if pose=="peek" else sprite(animal,pose,phase,direction,**kwargs)
                    put(image,animal,drawing,card,x,above,pose.upper())
                image.save(snapshots/f"{index+1:02}-{title.lower().replace(' ','-')}-{fraction}.jpg")
        return
    after=options.output.with_name("lil-buddy-motion-after.mp4")
    result=render_study(after,encoder,snapshots)
    metadata(after,encoder)
    compare(options.before,after,options.output,encoder,work)
    slow=options.output.with_name("lil-buddy-motion-before-after-slow.mp4")
    slow_motion(options.output,slow,encoder,work)
    audit=options.output.with_name("lil-buddy-motion-runtime-audit.mp4")
    audit_result=runtime_audit(audit,encoder,work)
    app.processEvents()
    for path in (after,options.output,slow,audit):
        subprocess.run([encoder,"-v","error","-i",str(path),"-map","0:v:0","-f","null","-"],check=True)
    report=dict(after=result,comparison=str(options.output),slow_motion=str(slow),audit=audit_result,
                source_reference_sha256=hashlib.sha256(options.before.read_bytes()).hexdigest(),
                renderer_sha256=hashlib.sha256((ROOT/"desktop_cat.py").read_bytes()).hexdigest(),
                full_decode="passed for all four videos",
                chapter_cuts="Intentional between study chapters; runtime audit shots are continuous.")
    (work/"export-report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))


if __name__=="__main__":
    main()
