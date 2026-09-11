# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the FreeCAD-OpenStudio bridge contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  See LICENSE for the full text.

"""Copy the view-side data of one .FCStd into another.

    python restore_view_data.py --from good.FCStd --into damaged.FCStd

Recovery tool for a document saved by a version of this bridge that discarded
GuiDocument.xml (FreeCAD builds no ViewProviders headless, so a scripted save
rewrote the archive without them).  fcbridge.save_document now preserves them,
so this is only needed to repair a file already affected.

Geometry is untouched: only entries absent from the target and not referenced
by its Document.xml are copied.  Objects created since the source was written
have no entry of their own and get FreeCAD's defaults on open.

Plain Python -- no FreeCAD import needed.
"""

import argparse
import os
import shutil
import zipfile


def view_entries(path):
    """Zip members that Document.xml does not reference, i.e. the view data."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if "Document.xml" not in names:
            raise SystemExit("%s is not an .FCStd (no Document.xml)" % path)
        doc_xml = zf.read("Document.xml").decode("utf-8", "replace")
        return {n: zf.read(n) for n in names
                if n != "Document.xml" and n not in doc_xml}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", required=True,
                    help="a copy that still has its view data")
    ap.add_argument("--into", dest="dst", required=True,
                    help="the document to repair (backed up first)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src, dst = os.path.abspath(args.src), os.path.abspath(args.dst)
    donor = view_entries(src)
    with zipfile.ZipFile(dst) as zf:
        present = set(zf.namelist())

    missing = [(n, d) for n, d in sorted(donor.items()) if n not in present]
    print("source view entries : %d" % len(donor))
    print("already in target   : %d" % (len(donor) - len(missing)))
    print("to restore          : %d" % len(missing))
    for name, data in missing:
        print("   %-28s %8d bytes" % (name, len(data)))

    if not missing:
        print("\nnothing to do.")
        return
    if args.dry_run:
        print("\n--dry-run: target not written.")
        return

    backup = dst + ".noview"
    if not os.path.exists(backup):
        shutil.copy2(dst, backup)
        print("\nbackup: %s" % os.path.basename(backup))

    tmp = dst + ".tmp"
    with zipfile.ZipFile(dst) as source, \
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for item in source.infolist():
            out.writestr(item, source.read(item.filename))
        for name, data in missing:
            out.writestr(name, data)
    os.replace(tmp, dst)

    with zipfile.ZipFile(dst) as zf:
        bad = zf.testzip()
    if bad:
        raise SystemExit("archive verification failed at %s" % bad)
    print("restored %d entries into %s (archive verified)"
          % (len(missing), os.path.basename(dst)))


if __name__ == "__main__":
    main()
