# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Tests for fcbridge.seed_view_defaults.

Run under the bridge venv:

    osvenv/Scripts/python.exe -m unittest discover -s tests -v

This function edits GuiDocument.xml inside a saved .FCStd, which is where every
display setting in the user's document lives.  Getting it wrong loses colours,
visibility, line widths and the camera for the whole document, so the two
things worth pinning down are that it only ever *adds* entries, and that it
leaves every other zip member byte-for-byte alone.

fcbridge imports FreeCAD at module scope and the venv has no FreeCAD, so the
modules it needs are stubbed -- seed_view_defaults itself touches none of them.
"""

import os
import sys
import types
import unittest
import zipfile
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for name in ("FreeCAD", "Part"):
    sys.modules.setdefault(name, types.ModuleType(name))
_bop = types.ModuleType("BOPTools")
_bop.SplitAPI = types.SimpleNamespace()
sys.modules.setdefault("BOPTools", _bop)

import fcbridge  # noqa: E402

GUI = """<?xml version='1.0' encoding='utf-8'?>
<Document SchemaVersion="1">
    <ViewProviderData Count="1">
        <ViewProvider name="Existing" expanded="0" treeRank="1">
            <Properties Count="1" TransientCount="0">
                <Property name="Transparency" type="App::PropertyPercent">
                    <Integer value="15"/>
                </Property>
            </Properties>
        </ViewProvider>
    </ViewProviderData>
</Document>
"""

OTHER = {
    "Document.xml": b"<Document/>",
    "Some.Shape.brp": b"\x00\x01\x02 not xml",
    "ShapeAppearance7": b"\x01\x00\x00\x00binary blob",
}


def write_fcstd(path, gui=GUI):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in OTHER.items():
            zf.writestr(name, data)
        if gui is not None:
            zf.writestr("GuiDocument.xml", gui)


def entries(path):
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("GuiDocument.xml"))
    data = root.find("ViewProviderData")
    out = {}
    for view in data.findall("ViewProvider"):
        props = {}
        for prop in view.find("Properties").findall("Property"):
            child = list(prop)
            if child:
                props[prop.get("name")] = child[0].get("value")
        out[view.get("name")] = props
    return int(data.get("Count")), out


class SeedViewDefaultsTest(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_seed_tmp.FCStd")
        write_fcstd(self.path)
        self.addCleanup(
            lambda: os.path.exists(self.path) and os.remove(self.path))

    def test_seeds_only_missing_entries(self):
        seeded = fcbridge.seed_view_defaults(self.path, ["Existing", "New"])
        self.assertEqual(seeded, ["New"])
        count, found = entries(self.path)
        self.assertEqual(count, 2)
        self.assertEqual(sorted(found), ["Existing", "New"])

    def test_does_not_touch_a_setting_the_user_made(self):
        fcbridge.seed_view_defaults(self.path, ["Existing", "New"])
        _, found = entries(self.path)
        self.assertEqual(found["Existing"]["Transparency"], "15")
        self.assertEqual(found["New"]["Transparency"], "70")

    def test_applies_every_default(self):
        fcbridge.seed_view_defaults(self.path, ["New"])
        _, found = entries(self.path)
        for name, _type, _tag, value in fcbridge.IMAGE_VIEW_DEFAULTS:
            self.assertEqual(found["New"][name], value)

    def test_is_idempotent(self):
        fcbridge.seed_view_defaults(self.path, ["New"])
        self.assertEqual(fcbridge.seed_view_defaults(self.path, ["New"]), [])
        count, found = entries(self.path)
        self.assertEqual(count, 2)
        self.assertEqual(len(found), 2)

    def test_count_matches_the_entries_actually_present(self):
        fcbridge.seed_view_defaults(self.path, ["A", "B", "C"])
        count, found = entries(self.path)
        self.assertEqual(count, len(found))
        self.assertEqual(count, 4)

    def test_leaves_other_members_byte_identical(self):
        fcbridge.seed_view_defaults(self.path, ["New"])
        with zipfile.ZipFile(self.path) as zf:
            for name, data in OTHER.items():
                self.assertEqual(zf.read(name), data, name)

    def test_no_gui_document_is_left_alone(self):
        write_fcstd(self.path, gui=None)
        self.assertEqual(fcbridge.seed_view_defaults(self.path, ["New"]), [])
        with zipfile.ZipFile(self.path) as zf:
            self.assertEqual(sorted(zf.namelist()), sorted(OTHER))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(
            fcbridge.seed_view_defaults(self.path + ".nope", ["New"]), [])


if __name__ == "__main__":
    unittest.main()
