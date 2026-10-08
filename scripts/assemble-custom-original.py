#!/usr/bin/env python3
"""Assemble the engineering Custom Edition 1.10 input root.

Extracts the stock Custom Edition 1.00 game file set from the supplied installer,
overlays the files produced by scripts/prepare-patched-client.sh, and writes a
manifest (path, size, SHA-256, origin). Installer-only payloads (NSIS plugins,
DirectX and redistributable setup programs) are excluded and listed.

Usage:
  .venv/bin/python scripts/assemble-custom-original.py --patched-run generated/patchwork/run-<id>
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'ref' / 'HaloCESetup.exe'
INSTALLER_SHA = os.environ.get('HALOPAD_INSTALLER_SHA', '150e430dc54ffb265cbe96605ef8909c9ba0065fa11bdbf170bfd88391cf98ba')  # another wrapper of the same 1.00 files (prepare-patched-client.sh checks the result)
PATCH_SHA = '33818f3f56b7dddc8c61d654af6567c9c5b9220ca75d6ac23a52611038257508'
EXCLUDED_TOP = ('$PLUGINSDIR', 'directx', 'redist', '.rsrc')   # .rsrc: a Setup.exe wrapper's own embedded cabinet
PATCH_TOOLING = {'haloupdate.exe', 'patch.rtp'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def installer_entries():
    out = subprocess.run(['7zz', 'l', '-slt', str(INSTALLER)], check=True,
                         capture_output=True, text=True).stdout
    paths = []
    for block in out.split('\n\n'):
        fields = dict(line.split(' = ', 1) for line in block.splitlines() if ' = ' in line)
        if 'Path' in fields and fields.get('Folder', '-') != '+' and fields['Path'] != str(INSTALLER):
            paths.append(fields['Path'])
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--patched-run', required=True, type=pathlib.Path)
    ap.add_argument('--out', type=pathlib.Path, default=ROOT / 'ref' / 'inputs' / 'custom-original')
    args = ap.parse_args()

    if sha256(INSTALLER) != INSTALLER_SHA:
        sys.exit('FAIL: installer hash differs from the recorded supplied installer')
    patched = args.patched_run.resolve() / 'files'
    if not (patched / 'haloce.exe').is_file():
        sys.exit(f'FAIL: {patched} does not contain a patched haloce.exe')
    if args.out.exists():
        sys.exit(f'FAIL: {args.out} already exists; refusing to overwrite an input root')

    entries = installer_entries()
    excluded = sorted(p for p in entries if p.split('/', 1)[0] in EXCLUDED_TOP)
    wanted = [p for p in entries if p.split('/', 1)[0] not in EXCLUDED_TOP]

    with tempfile.TemporaryDirectory(dir=ROOT / 'generated') as tmp:
        stage = pathlib.Path(tmp) / 'custom-original'
        stage.mkdir()
        listfile = pathlib.Path(tmp) / 'wanted.txt'
        listfile.write_text('\n'.join(wanted) + '\n')
        subprocess.run(['7zz', 'x', '-y', f'-o{stage}', str(INSTALLER), f'@{listfile}'],
                       check=True, capture_output=True)

        installer_hash = {}
        for p in stage.rglob('*'):
            if p.is_file():
                installer_hash[p.relative_to(stage).as_posix().lower()] = sha256(p)
        missing = [p for p in wanted if p.lower() not in installer_hash]
        if missing:
            sys.exit(f'FAIL: extraction missed {missing[:5]}')

        # Overlay patched files. The file system is case-insensitive, so match
        # existing paths case-insensitively and keep the patched file's name.
        existing = {p.relative_to(stage).as_posix().lower(): p for p in stage.rglob('*')}
        for src in sorted(q for q in patched.rglob('*') if q.is_file()):
            rel = src.relative_to(patched).as_posix()
            if rel in PATCH_TOOLING:
                continue
            key = rel.lower()
            if key in existing and existing[key].is_file():
                dst = existing[key]
                if dst.name != src.name:
                    dst.unlink()
                    dst = dst.with_name(src.name)
            else:
                parent_key = str(pathlib.PurePosixPath(key).parent)
                parent = existing.get(parent_key, stage / pathlib.PurePosixPath(rel).parent)
                parent.mkdir(parents=True, exist_ok=True)
                dst = parent / src.name
            shutil.copy2(src, dst)

        files = []
        for p in sorted((q for q in stage.rglob('*') if q.is_file()), key=lambda q: str(q).lower()):
            rel = p.relative_to(stage).as_posix()
            h = sha256(p)
            origin = 'installer' if installer_hash.get(rel.lower()) == h else 'patch'
            files.append({'path': rel, 'size': p.stat().st_size, 'sha256': h, 'origin': origin})

        manifest = {
            'schema': 1,
            'profile': 'custom-en-1.0.10.0621',
            'created_utc': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'installer_sha256': INSTALLER_SHA,
            'patch_sha256': PATCH_SHA,
            'patched_run': str(args.patched_run),
            'excluded_installer_entries': excluded,
            'file_count': len(files),
            'files': files,
        }
        (stage / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(args.out))

    for p in args.out.rglob('*'):
        if p.is_file():
            os.chmod(p, 0o444)

    evid = ROOT / 'docs' / 'artifacts' / datetime.date.today().isoformat() / 'G1a' / 'assemble'
    evid.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.out / 'MANIFEST.json', evid / 'manifest.json')
    patched_count = sum(1 for f in files if f['origin'] == 'patch')
    print(f'PASS assembled {len(files)} files ({patched_count} from patch) into {args.out}')
    print(f'excluded {len(excluded)} installer-only entries; manifest copied to {evid}')
    exe = next(f for f in files if f['path'].lower() == 'haloce.exe')
    print('haloce.exe', exe['sha256'])


if __name__ == '__main__':
    main()

