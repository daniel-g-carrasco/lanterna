# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build for Windows, under MSYS2 UCRT64 (the only MSYS2
# environment with libadwaita). Run from the repository root:
#   python tools/compile_po.py && python tools/make_ico.py && pyinstaller --noconfirm build-aux/windows/lanterna.spec
# A spec file, not flags: the gi hook collects GTK 3 unless told otherwise.
import shutil
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent.parent
prefix = Path(sys.prefix)              # MSYS2: C:/msys64/ucrt64

datas = [(str(ROOT / 'data' / 'icons'), 'share/icons'), (str(ROOT / 'LICENSE'), 'licenses')]
binaries = []

if (ROOT / 'locale').is_dir():
    datas.append((str(ROOT / 'locale'), 'share/locale'))
else:
    print('WARNING: locale/ missing: run tools/compile_po.py first (English only)')

# gdbus.exe lets GLib start a session bus, which GApplication needs to keep
# one instance running (a second launch hands its files to the first)
gdbus = shutil.which('gdbus') or str(prefix / 'bin' / 'gdbus.exe')
if Path(gdbus).is_file():
    binaries.append((gdbus, '.'))
else:
    print('WARNING: gdbus.exe not found: every launch would open another window')

# CA bundle for the app's own HTTPS calls (the update check): MSYS2's OpenSSL
# does not always see the Windows certificate store
for ca in (prefix / 'etc' / 'ssl' / 'certs' / 'ca-bundle.crt', prefix / 'etc' / 'ssl' / 'cert.pem'):
    if ca.is_file():
        datas.append((str(ca), 'certs'))
        break
else:
    print('WARNING: no CA bundle found: the update check may fail on TLS')

a = Analysis(
    [str(Path(SPECPATH) / 'lanterna_main.py')],
    pathex=[str(ROOT / 'src')],
    binaries=binaries,
    datas=datas,
    # imported only on Windows, at run time: the analysis does not see them
    hiddenimports=['lanterna.win32', 'lanterna.wintoast', 'lanterna.minisign'],
    hooksconfig={'gi': {'module-versions': {'Gtk': '4.0', 'Gdk': '4.0', 'Adw': '1', 'GdkPixbuf': '2.0',
                                            'Graphene': '1.0', 'Gsk': '4.0'}}},
    excludes=['tkinter'],
)

sys.path.insert(0, str(ROOT / 'tools'))
from package_inventory import inventory

pure = [row for row in a.pure if row[1] != '-']
_, notices = inventory([*a.binaries, *a.datas, *pure, *a.scripts],
                       prefix.parent, ROOT, Path(workpath),
                       revision=os.environ.get('GITHUB_SHA', 'dev'))
a.datas += notices

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='Lanterna',
    console=False,
    icon=str(ROOT / 'build-aux' / 'windows' / 'lanterna.ico'),
    manifest=str(Path(SPECPATH) / 'lanterna.manifest'),
)

coll = COLLECT(exe, a.binaries, a.datas, name='Lanterna')
