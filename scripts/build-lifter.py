#!/usr/bin/env python3
"""Build SRW OUT_LLASM and llasm from pinned sources in a new private directory."""
import datetime
import hashlib
import json
import platform
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

def tree_digest(base):
    entries = []
    for p in sorted(base.rglob('*')):
        if p.is_symlink():
            entries.append([p.relative_to(base).as_posix(), 'link', os.readlink(p)])
        elif p.is_file():
            entries.append([p.relative_to(base).as_posix(), 'file', digest(p)])
    return hashlib.sha256(json.dumps(entries, separators=(',', ':')).encode()).hexdigest()

def ldc_toolchain(lock):
    if os.environ.get('HALOPAD_LDC2'):              # a local LDC (Intel Macs: the osx-x86_64 release of the pinned version)
        return pathlib.Path(os.environ['HALOPAD_LDC2'])
    spec = lock['ldc']
    parent = ROOT / 'generated/toolchains'
    parent.mkdir(parents=True, exist_ok=True)
    base = parent / spec['root']
    if not base.exists():
        archive = parent / spec['archive']
        if not archive.exists():
            # Exclusive destination; a failed partial download is preserved for diagnosis.
            with urllib.request.urlopen(spec['url'], timeout=60) as response, archive.open('xb') as out:
                shutil.copyfileobj(response, out)
        if digest(archive) != spec['sha256']:
            raise RuntimeError('LDC archive hash mismatch; preserved without extraction')
        with tarfile.open(archive) as tf:
            seen = set()
            for m in tf.getmembers():
                p = (parent / m.name).resolve()
                if parent.resolve() not in p.parents or m.isdev() or m.isfifo():
                    raise RuntimeError('unsafe compiler archive entry')
                if m.name in seen:
                    raise RuntimeError('duplicate compiler archive entry')
                seen.add(m.name)
                if m.issym() or m.islnk():
                    link = (p.parent / m.linkname).resolve() if m.issym() else (parent / m.linkname).resolve()
                    if parent.resolve() not in link.parents:
                        raise RuntimeError('unsafe compiler archive link')
            tf.extractall(parent)
    if base.is_symlink() or tree_digest(base) != spec['tree_sha256']:
        raise RuntimeError('LDC extracted source/tool tree identity mismatch')
    return base / 'bin/ldc2'

def main():
    if len(sys.argv) != 1:
        raise RuntimeError('usage: scripts/build-lifter.sh (no game-input arguments)')
    lock = json.loads((ROOT / 'toolchains.lock.json').read_text())
    env_python = ROOT / '.venv/bin/python'
    subprocess.run([str(env_python), '-c', 'import SCons; assert SCons.__version__ == ' + repr(lock['scons'])], check=True)
    subprocess.run(['sh', str(ROOT / 'scripts/verify-sources.sh')], check=True)
    ldc = ldc_toolchain(lock)
    patch = ROOT / 'port/patches/srw-macos-llasm.patch'
    identity = {'source_lock': digest(ROOT/'dependencies.lock.json'),
                'toolchain_lock': digest(ROOT/'toolchains.lock.json'), 'patch': digest(patch),
                'builder': digest(pathlib.Path(__file__)),
                'clang': subprocess.check_output(['clang', '--version'], text=True),
                'sdk': subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-build-version'], text=True)}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    runid = key[:12] + '-' + uuid.uuid4().hex[:8]
    build = ROOT / 'generated/tool-builds' / runid
    evidence = ROOT / 'docs/artifacts' / datetime.date.today().isoformat() / 'G0' / ('lifter-' + runid)
    evidence.mkdir(parents=True)
    build.mkdir(parents=True)
    manifest = {'identity': identity, 'key': key, 'result': 'BUILDING', 'build': str(build.relative_to(ROOT)), 'evidence': str(evidence.relative_to(ROOT))}
    (evidence / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    try:
        shutil.copytree(ROOT/'ref/sr/SRW', build/'SRW')
        shutil.copytree(ROOT/'ref/sr/llasm', build/'llasm')
        commands = [(['patch', '-p1', '-i', str(patch)], build/'SRW'),
                    ([str(ROOT/'.venv/bin/scons'), '-j4'], build/'SRW'),
                    ([str(ldc), '-O2', 'llasm.d', '-of=llasm'], build/'llasm')]
        with (evidence/'build.log').open('x') as log:
            for args, cwd in commands:
                log.write('$ '+json.dumps(args)+'\n'); log.flush()
                subprocess.run(args, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, check=True)
        binaries = [build/'SRW/SRW.exe', build/'llasm/llasm']
        for binary in binaries:
            arch = subprocess.check_output(['lipo', '-archs', str(binary)], text=True).strip()
            if arch != platform.machine():           # the tools run on this Mac (arm64, or x86_64 on Intel)
                raise RuntimeError('build is not exclusively ' + platform.machine())
        manifest.update(result='BUILT_NOT_HALO_VALIDATED', artifacts={str(p.relative_to(build)):digest(p) for p in binaries})
        (build/'build-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        print('BUILT:', build.relative_to(ROOT))
        print('Evidence:', evidence.relative_to(ROOT))
        print('Run scripts/test-lifter-smoke.py --build', build.relative_to(ROOT))
    except Exception:
        manifest['result'] = 'FAIL'
        raise
    finally:
        (evidence/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print('FAIL:', exc, file=sys.stderr)
        sys.exit(1)
