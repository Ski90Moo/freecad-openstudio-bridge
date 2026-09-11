# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

import sys
from PIL import Image
Image.MAX_IMAGE_PIXELS=None
import elev, render
name,u0,u1,z0,z1 = sys.argv[1], *map(float,sys.argv[2:6])
out = sys.argv[6] if len(sys.argv)>6 else "z.png"
u,z = elev.mappings()[name]
grid,axis = elev.ELEV_GRID[name]
ks=list(grid); import bisect
# invert the linear maps numerically
xs=[grid[k] for k in ks]
lo,hi=min(xs)-800,max(xs)+800
def inv_u(target):
    a,b=lo,hi
    for _ in range(80):
        m=(a+b)/2
        if (u(m)-target)*(u(a)-target)<=0: b=m
        else: a=m
    return (a+b)/2
def inv_z(target): return elev.DATUM[name]+target*elev.PT
xa,xb=sorted([inv_u(u0),inv_u(u1)])
ya,yb=sorted([inv_z(z0),inv_z(z1)])
c0,r1=render.px_of(xa,ya); c1,r0=render.px_of(xb,yb)
im=Image.open("draw.png").crop((int(c0),int(r0),int(c1),int(r1)))
print("%s u %g..%g ft  z %g..%g ft -> %s px"%(name,u0,u1,z0,z1,im.size))
w=min(1400, im.width)
im=im.resize((w,max(1,int(im.height*w/im.width))), Image.LANCZOS)
im.save(out); print("wrote",out,im.size)
