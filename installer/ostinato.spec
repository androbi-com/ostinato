# -*- mode: python -*-
# SPDX-License-Identifier: GPL-3.0-only
# PyInstaller: one folder with two programs sharing Python and the libraries.
#   ostinato-cli.exe  console: the device server, `ostinato-cli.exe mcp` (what Claude starts), claude-config ...
#   Ostinato.exe   windowed: the tray app
# Built by tools/build_windows.ps1 (from the repository's root): pyinstaller installer/ostinato.spec
import os

from PyInstaller.utils.hooks import collect_data_files, collect_delvewheel_libs_directory, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
ICON = os.path.join(ROOT, "build", "ostinato.ico")  # (written by the build script)

hidden = (collect_submodules("ostinato") + collect_submodules("uvicorn") + collect_submodules("mcp")
          + collect_submodules("pystray") + ["rtmidi", "mido.backends.rtmidi"])
datas = (collect_data_files("ostinato")  # the page, the algorithms' and backends' defaults, the simulator's data
         + collect_data_files("verovio")  # its fonts and resources: the staffs
         + collect_data_files("music21", excludes=["corpus/**"])  # (not its sheet-music library: unused, 90 MB)
         + collect_data_files("resvg_py"))
excludes = ["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide6"]

# verovio's wheel (delvewheel) loads a renamed C++ runtime from site-packages\verovio.libs\: keep that folder
_, binaries = collect_delvewheel_libs_directory("verovio")


def analysis(script):
    return Analysis([os.path.join(ROOT, "installer", script)], pathex=[SRC], hiddenimports=hidden, datas=datas,
                    binaries=binaries, excludes=excludes, noarchive=False)


a_cli = analysis("entry_cli.py")
a_tray = analysis("entry_tray.py")

exe_cli = EXE(PYZ(a_cli.pure), a_cli.scripts, [], exclude_binaries=True, name="ostinato-cli", console=True,
              icon=ICON, upx=False)
exe_tray = EXE(PYZ(a_tray.pure), a_tray.scripts, [], exclude_binaries=True, name="Ostinato", console=False,
               icon=ICON, upx=False)

coll = COLLECT(exe_cli, a_cli.binaries, a_cli.datas, exe_tray, a_tray.binaries, a_tray.datas,
               upx=False, name="Ostinato")
