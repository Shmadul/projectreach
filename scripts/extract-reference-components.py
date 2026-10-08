#!/usr/bin/env python3
"""Extract components the Halo Custom Edition installer puts on the reference machine.

Halo's installer (ref/HaloCESetup.exe, Nullsoft) ships redist/msxmlenu.msi, the MSXML 4.0
SP2 redistributable: Keystone.dll (the multiplayer chat UI) parses its .ksml layouts with
MSXML 4.0 (CLSID_DOMDocument40) and has no fallback. This extracts msxml4.dll and its
resource DLL msxml4r.dll from that MSI's XML_Core.cab into the ignored
ref/inputs/reference-machine/system32/, where Windows would have them, and checks them
against the hashes pinned here. Needs 7-Zip (7zz). Nothing is executed.

Usage: .venv/bin/python scripts/extract-reference-components.py
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'ref' / 'HaloCESetup.exe'
INSTALLER_SHA256 = os.environ.get('HALOPAD_INSTALLER_SHA', '150e430dc54ffb265cbe96605ef8909c9ba0065fa11bdbf170bfd88391cf98ba')  # another wrapper of the same 1.00 files (prepare-patched-client.sh checks the result)
OUT = ROOT / 'ref' / 'inputs' / 'reference-machine' / 'system32'
# (file in XML_Core.cab, installed name, SHA-256)
FILES = [('msxml4.dll.sxs.2E8D8EBB_CC16_45E1_BBCA_CB1ED881EDB7', 'msxml4.dll',
          '9808f05f0da3775f07a88fd614f038e6f4ac5ff680c73d5fd40116a5a247a53d'),
         ('msxml4r.dll.sxs.2E8D8EBB_CC16_45E1_BBCA_CB1ED881EDB7', 'msxml4r.dll',
          '4e75a8e8cbc8486cafc73b208532c6969c23731ecf0c93e66a4ac076136de750')]


def sha(p):
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def main():
    if sha(INSTALLER) != INSTALLER_SHA256:
        sys.exit('FAIL: ref/HaloCESetup.exe is not the accepted installer')
    if all((OUT / name).is_file() and sha(OUT / name) == want for _, name, want in FILES):
        print('already extracted (pinned hashes match):', OUT.relative_to(ROOT))
        return 0
    zz = shutil.which('7zz') or shutil.which('7z')
    if not zz:
        sys.exit('FAIL: 7-Zip (7zz) is needed')
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        run = lambda *a: subprocess.run([zz, *a], check=True, capture_output=True)
        run('e', '-y', f'-o{t}', str(INSTALLER), 'redist/msxmlenu.msi')
        run('e', '-y', f'-o{t}', str(t / 'msxmlenu.msi'), 'XML_Core.cab')
        run('e', '-y', f'-o{t}', str(t / 'XML_Core.cab'), *[f for f, _, _ in FILES])
        for cab_name, name, want in FILES:
            got = sha(t / cab_name)
            if got != want:
                sys.exit(f'FAIL: {name} from the installer has SHA-256 {got}, expected {want}')
            shutil.copyfile(t / cab_name, OUT / name)
            print(f'{name}  {want}')
    print('extracted to', OUT.relative_to(ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
