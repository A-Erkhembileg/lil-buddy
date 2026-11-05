#!/usr/bin/env python3
"""Offline key-pose registration for the original approved drawings.

Optional numpy/OpenCV are authoring dependencies only. Runtime loads PNGs.
Unlike optical flow, the shared semantic mesh registers eyes, ears, muzzle,
chest, hips and paws explicitly. Only one drawing contributes per sample;
there are no translucent limbs or independently generated replacement faces.
"""
import json
import os
from pathlib import Path
import sys
os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import cv2
from PIL import Image
from PyQt6.QtWidgets import QApplication
import desktop_cat as pet
from tools.bake_motion_atlases import pil_image

# Near/far ear, muzzle, chin, near/far eye, chest, back, hip, belly,
# near/far forepaw, hind paw. Shared connectivity avoids feature duplication.
LANDMARKS = {'Cat': {'idle': [[222, 40], [307, 22], [323, 122], [311, 153], [280, 102], [326, 91], [263, 191],
                  [180, 172], [165, 249], [225, 276], [277, 304], [312, 304], [216, 310]],
         'stand': [[264, 25], [343, 15], [360, 101], [346, 130], [322, 85], [365, 76], [280, 203],
                   [162, 164], [145, 241], [184, 258], [285, 304], [311, 304], [143, 305]],
         'groom': [[178, 98], [284, 72], [272, 164], [260, 188], [225, 155], [282, 139], [250, 222],
                   [195, 190], [181, 258], [230, 277], [274, 211], [287, 307], [215, 311]],
         'settle': [[207, 141], [324, 178], [228, 250], [265, 277], [214, 222], [268, 240], [275, 280],
                    [168, 165], [152, 220], [185, 269], [285, 286], [304, 284], [233, 288]]},
 'Dog': {'idle': [[218, 37], [283, 40], [302, 120], [288, 154], [256, 108], [304, 103], [257, 204],
                  [171, 211], [156, 263], [210, 285], [275, 306], [312, 307], [215, 306]],
         'stand': [[252, 15], [309, 12], [355, 105], [334, 146], [303, 85], [337, 80], [253, 208],
                   [130, 182], [96, 237], [156, 253], [242, 304], [268, 303], [115, 306]],
         'groom': [[174, 91], [237, 89], [199, 184], [204, 221], [180, 163], [225, 163], [217, 242],
                   [171, 217], [173, 270], [226, 291], [204, 240], [252, 307], [281, 308]],
         'settle': [[229, 134], [312, 179], [226, 250], [247, 266], [214, 212], [270, 233], [234, 280],
                    [178, 171], [149, 222], [183, 263], [272, 284], [295, 280], [234, 290]]},
 'Fox': {'idle': [[242, 30], [286, 26], [334, 111], [313, 137], [274, 100], [307, 95], [269, 207],
                  [196, 192], [191, 261], [239, 282], [269, 307], [296, 307], [227, 306]],
         'stand': [[267, 13], [312, 16], [370, 110], [347, 132], [312, 99], [345, 93], [294, 214],
                   [174, 183], [155, 242], [213, 261], [299, 307], [321, 307], [177, 307]],
         'groom': [[257, 28], [312, 38], [335, 143], [308, 160], [285, 115], [324, 118], [278, 211],
                   [201, 193], [194, 267], [251, 290], [329, 172], [292, 305], [240, 308]],
         'settle': [[220, 83], [288, 89], [281, 211], [286, 224], [258, 184], [300, 197], [255, 274],
                    [178, 166], [159, 221], [197, 269], [284, 282], [311, 277], [238, 293]]}}
P = LANDMARKS

def warp(image,source,dest,triangles):
 image=np.array(image.convert('RGBa'),np.float32)
 out=np.zeros((320,400,4),np.float32)
 for indices in triangles:
  src=np.float32([source[i] for i in indices]);dst=np.float32([dest[i] for i in indices])
  transform=cv2.getAffineTransform(src,dst)
  patch=cv2.warpAffine(image,transform,(400,320),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)
  mask=np.zeros((320,400),np.uint8);cv2.fillConvexPoly(mask,np.int32(np.rint(dst)),255)
  out[mask!=0]=patch[mask!=0]
 return out

def frames(animal,source,target,kind,count=19):
 outer=[(0,0),(200,0),(399,0),(399,160),(399,319),(200,319),(0,319),(0,160)]
 # Tail topology changes on curling; don't force its tip across the face.
 a=np.float32(P[animal]['idle'][:13]+outer);b=np.float32(P[animal][kind][:13]+outer)
 avg=(a+b)/2
 sub=cv2.Subdiv2D((0,0,400,320))
 for xy in avg:sub.insert(tuple(map(float,xy)))
 tri=[]
 for points in sub.getTriangleList().reshape(-1,3,2):
  indices=[]
  for xy in points:
   distance=np.linalg.norm(avg-xy,axis=1);indices.append(int(np.argmin(distance)))
  if len(set(indices))==3 and all(np.min(np.linalg.norm(avg-xy,axis=1))<.1 for xy in points):tri.append(indices)
 out=[]
 for i in range(count):
  t=i/(count-1);dest=a*(1-t)+b*t
  if i==0:out.append(source);continue
  if i==count-1:out.append(target);continue
  old=warp(source,a,dest,tri);new=warp(target,b,dest,tri)
  # Select one registered drawing at a time. Crossfading these independent
  # silhouettes creates translucent extra legs and a second face.
  pixels=np.rint(old if t<.6 else new).clip(0,255).astype(np.uint8)
  out.append(Image.fromarray(pixels,'RGBa').convert('RGBA'))
 return out

def bake():
    app=QApplication.instance() or QApplication([])
    for animal in P:
        bases,clips=pet._sprite_bases(animal)
        idle=pil_image(pet._sprite_tinted(animal,clips['idle'][0],None))
        targets={'stand':pil_image(pet._articulated_image(animal,'land',1,None)),
                 'groom':pil_image(pet._sprite_tinted(animal,clips['groom'][0],None)),
                 'settle':pil_image(pet._sprite_tinted(animal,clips['sleep'][0],None))}
        all_frames=[];sequences={}
        for name,target in targets.items():
            sequence=frames(animal,idle,target,name,25)
            reverse={'stand':'sit','groom':'ungroom','settle':'wake'}[name]
            sequences[name]=list(range(len(all_frames),len(all_frames)+len(sequence)))
            sequences[reverse]=sequences[name][::-1]
            all_frames.extend(sequence)
        columns=8
        atlas=Image.new('RGBA',(columns*400,((len(all_frames)+columns-1)//columns)*320))
        for i,frame in enumerate(all_frames):atlas.paste(frame,(i%columns*400,i//columns*320))
        folder=ROOT/'artwork'/'motion';filename=animal.lower()+'-transitions.png'
        atlas.save(folder/filename)
        (folder/(animal.lower()+'-transitions.json')).write_text(json.dumps(dict(
            format_version=1,image=filename,cell_size=[400,320],columns=columns,
            method='single-source semantic mesh; original key drawings',clips=sequences),indent=2)+'\n')
        print(animal,len(all_frames),flush=True)
    app.processEvents()

if __name__=='__main__':bake()
