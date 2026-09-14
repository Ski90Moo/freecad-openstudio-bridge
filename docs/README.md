# Images

| File | Where it is used |
|---|---|
| `hero.png` | The banner at the top of the main [README](../README.md) |
| `social-preview.png` | The repository's social preview — the card that renders when a link to this repo is posted. Set under **Settings → General → Social preview**, which is the one repository setting GitHub has never exposed through its API, so it has to be uploaded by hand. |

Both are the same FreeCAD viewport screenshot of a built model read back in by
`fc_import_surfaces.py` and shaded by `colorize.FCMacro`. `make_images.py`
crops it to its content, scales it, and — for the social preview — sets it on
a card painted the screenshot's own background colour, so the join between the
render and the card is invisible.

To regenerate after the model changes, screenshot the geometry document and:

```
osvenv/Scripts/python.exe docs/make_images.py shot.png docs/hero.png --hero
osvenv/Scripts/python.exe docs/make_images.py shot.png docs/social-preview.png
```

There is a `--light` variant for a light background. It keeps the render as an
inset panel rather than knocking the dark ground out of it, because the model's
edges are drawn in black on a near-black viewport: no colour-distance mask can
tell a black edge from the background, and every outline comes out dashed.
