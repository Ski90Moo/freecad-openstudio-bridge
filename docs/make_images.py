# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Compose a FreeCAD viewport screenshot into a 1280x640 GitHub social preview.

GitHub crops social previews to 2:1; the raw screenshot is 2.53:1, so it is
auto-cropped to its content, scaled to fit, and set on a card painted the
screenshot's own background colour -- (31,31,31), FreeCAD's dark viewport --
so there is no visible seam between the render and the card.

Take the screenshot from the geometry document with colorize.FCMacro applied,
looking along the long axis so the two-storey end and the warehouse both read.

    python make_images.py shot.png hero.png --hero        # README banner
    python make_images.py shot.png social-preview.png     # 1280x640 card
    python make_images.py shot.png social-light.png --light

Needs Pillow, and reads its fonts from the Windows font directory; on another
platform point FONTS at one that has a bold, a semilight and a monospace face.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONTS = "C:/Windows/Fonts"
W, H = 1280, 640
SS = 2                       # supersample the text, then downsample

DARK = {
    "bg": (31, 31, 31),
    "ink": (240, 240, 240),
    "dim": (150, 150, 150),
    "faint": (104, 104, 104),
}
LIGHT = {
    "bg": (247, 248, 250),
    "ink": (24, 28, 36),
    "dim": (92, 102, 118),
    "faint": (142, 152, 168),
}

TITLE = "FreeCAD  \u2194  OpenStudio  geometry bridge"
TAG = "Draw the floor plan once in FreeCAD; read exact coordinates out of it."


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def content_box(image, background, tol=6):
    """Bounding box of everything that is not the flat background."""
    grey = Image.new("RGB", image.size, background)
    from PIL import ImageChops
    diff = ImageChops.difference(image.convert("RGB"), grey).convert("L")
    return diff.point(lambda v: 255 if v > tol else 0).getbbox()


def compose(source, out, theme):
    shot = Image.open(source).convert("RGB")
    box = content_box(shot, DARK["bg"])
    if box:
        pad = 6
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(shot.width, box[2] + pad), min(shot.height, box[3] + pad))
        shot = shot.crop(box)

    card = Image.new("RGB", (W * SS, H * SS), theme["bg"])

    # Reserve the lower strip for the wordmark; fit the render above it.
    # The render is wider than it is tall, so this is width-limited: the
    # height allowance only has to be generous enough not to bind.
    avail_w, avail_h = int(W * 0.985), int(H * 0.80)
    scale = min(avail_w / shot.width, avail_h / shot.height)
    size = (int(shot.width * scale * SS), int(shot.height * scale * SS))
    render = shot.resize(size, Image.LANCZOS)

    # The render keeps its own dark ground in both themes.  Knocking it out
    # for the light card was tried and abandoned: the model's edges are drawn
    # in black on a near-black viewport, so no colour-distance mask can tell
    # a black edge from the background, and every outline came out dashed.
    # On a light card it therefore sits as an inset panel, which is honest
    # about being a screenshot and costs nothing.
    at = ((W * SS - size[0]) // 2, int(H * 0.035 * SS))
    card.paste(render, at)
    if theme is not DARK:
        ImageDraw.Draw(card).rectangle(
            [at[0], at[1], at[0] + size[0] - 1, at[1] + size[1] - 1],
            outline=(214, 219, 227), width=SS)

    art = ImageDraw.Draw(card)
    big = font("segoeuib.ttf", int(44 * SS))
    mid = font("segoeuisl.ttf" if theme is DARK else "segoeui.ttf",
               int(23 * SS))
    small = font("consola.ttf", int(15 * SS))

    # The building's silhouette runs diagonally, leaving the lower left
    # clear, so the wordmark sits up against it rather than at the margin.
    x = int(54 * SS)
    y = int(H * 0.735 * SS)
    art.text((x, y), TITLE, font=big, fill=theme["ink"])
    art.text((x, y + int(58 * SS)), TAG, font=mid, fill=theme["dim"])
    art.text((W * SS - x, y + int(8 * SS)), "GPL-3.0", font=small,
             fill=theme["faint"], anchor="ra")

    card.resize((W, H), Image.LANCZOS).save(out)
    print("%s  %dx%d  %.0f KB"
          % (out, W, H, os.path.getsize(out) / 1024.0))


def hero(source, out, width=1600):
    """The render alone, cropped and scaled -- for the top of the README.

    No wordmark: the README carries its own heading, and a second one baked
    into the image would only disagree with it the first time either changes.
    """
    shot = Image.open(source).convert("RGB")
    box = content_box(shot, DARK["bg"])
    if box:
        pad = 10
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(shot.width, box[2] + pad), min(shot.height, box[3] + pad))
        shot = shot.crop(box)
    height = int(round(shot.height * width / float(shot.width)))
    shot.resize((width, height), Image.LANCZOS).save(out, optimize=True)
    print("%s  %dx%d  %.0f KB"
          % (out, width, height, os.path.getsize(out) / 1024.0))


if __name__ == "__main__":
    src = sys.argv[1]
    dst = sys.argv[2]
    if "--hero" in sys.argv:
        hero(src, dst)
    else:
        compose(src, dst, LIGHT if "--light" in sys.argv else DARK)
