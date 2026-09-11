# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Draw the derived openings back onto the elevation they came from."""
import sys
from PIL import Image, ImageDraw
Image.MAX_IMAGE_PIXELS=None
import elev, render, openings as O

name=sys.argv[1]; out=sys.argv[2]
res=O.run(report=False)[name]
u,z=elev.mappings()[name]
grid,_=elev.ELEV_GRID[name]
xs=[grid[k] for k in grid]; lo,hi=min(xs)-900,max(xs)+900
def inv_u(t):
    a,b=lo,hi
    for _ in range(80):
        m=(a+b)/2
        if (u(m)-t)*(u(a)-t)<=0: b=m
        else: a=m
    return (a+b)/2
def inv_z(t): return elev.DATUM[name]+t*elev.PT
x0,x1,y0,y1 = elev.__dict__.get("_",None) or (0,0,0,0)
R = {"NORTH":(400,1990,1180,1420),"WEST":(460,1025,690,1010),
     "EAST":(1400,1975,690,1010),"SOUTH":(420,2030,190,460)}[name]
c0,r1=render.px_of(R[0],R[2]); c1,r0=render.px_of(R[1],R[3])
im=Image.open("draw.png").crop((int(c0),int(r0),int(c1),int(r1))).convert("RGB")
d=ImageDraw.Draw(im)
COL={"FixedWindow":(0,110,255),"Door":(0,170,0),"GlassDoor":(230,120,0),"OverheadDoor":(210,0,0)}
for c in res["kept"]:
    ax,bx=sorted([inv_u(c["u0"]),inv_u(c["u1"])])
    ay,by=inv_z(c["z0"]),inv_z(c["z1"])
    p0=render.px_of(ax,by); p1=render.px_of(bx,ay)
    box=[p0[0]-c0,p0[1]-r0,p1[0]-c0,p1[1]-r0]
    d.rectangle(box, outline=COL[c["type"]], width=14)
sc=min(1.0, 2000/im.width)
im=im.resize((int(im.width*sc),int(im.height*sc)), Image.LANCZOS)
im.save(out); print("wrote",out,im.size,"-",len(res["kept"]),"openings")
