#!/usr/bin/env python3
"""Execute a self-authored PE arithmetic fixture through the built AOT tools.
This validates tool wiring only, not Halo or the full CPU/memory contract.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import struct
import subprocess
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def make_pe(path):
    # mov eax,[esp+4]; mov ecx,[esp+8]; add eax,ecx; xor eax,5a5a5a5a; ret.
    # No absolute code/data addresses and therefore no relocation entries.
    code = bytes.fromhex('8b4424048b4c240801c8355a5a5a5ac3')
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x14c, 1, 0, 0, 0, 224, 0x103)
    o = 0x98
    struct.pack_into('<HBBIIIIII', data, o, 0x10b, 7, 10, 512, 0, 0, 0x1000, 0x1000, 0)
    struct.pack_into('<III', data, o+28, 0x400000, 0x1000, 0x200)
    struct.pack_into('<HHHHHH', data, o+40, 4, 0, 0, 0, 4, 0)
    struct.pack_into('<IIIHHIIIIII', data, o+56, 0x2000, 0x200, 0, 3, 0, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    struct.pack_into('<8sIIIIIIHHI', data, o+224, b'.text\0\0\0', len(code), 0x1000, 512, 512, 0, 0, 0, 0, 0x60000020)
    data[512:512+len(code)] = code
    path.write_bytes(data)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=pathlib.Path, required=True)
    args = parser.parse_args()
    build = args.build.resolve()
    if (ROOT/'generated/tool-builds').resolve() not in build.parents:
        raise RuntimeError('build must be an ignored local tool build')
    manifest = json.loads((build/'build-manifest.json').read_text())
    if manifest['result'] != 'BUILT_NOT_HALO_VALIDATED':
        raise RuntimeError('tool build did not complete')
    for name, digest in manifest['artifacts'].items():
        if name not in ['SRW/SRW.exe', 'llasm/llasm'] or sha(build/name) != digest:
            raise RuntimeError('tool binary identity mismatch')
    if set(manifest['artifacts']) != {'SRW/SRW.exe', 'llasm/llasm'}:
        raise RuntimeError('incomplete tool artifacts')
    for key, name in [('source_lock','dependencies.lock.json'),('toolchain_lock','toolchains.lock.json'),('patch','port/patches/srw-macos-llasm.patch'),('builder','scripts/build-lifter.py')]:
        if manifest['identity'][key] != sha(ROOT/name):
            raise RuntimeError('stale build identity: '+key)
    if manifest['identity']['clang'] != subprocess.check_output(['clang','--version'],text=True):
        raise RuntimeError('compiler identity changed since tool build')
    if manifest['identity']['sdk'] != subprocess.check_output(['xcrun','--sdk','macosx','--show-sdk-build-version'],text=True):
        raise RuntimeError('SDK identity changed since tool build')
    subprocess.run(['sh', str(ROOT/'scripts/verify-sources.sh')], check=True)
    runid = uuid.uuid4().hex[:12]
    work = ROOT/'generated/fixtures'/('lifter-'+runid)
    evidence = ROOT/'docs/artifacts'/datetime.date.today().isoformat()/'G0'/('lifter-smoke-'+runid)
    work.mkdir(parents=True); evidence.mkdir(parents=True)
    result = {'result':'RUNNING', 'scope':'SELF_AUTHORED_TOOL_SMOKE_ONLY', 'build_key':manifest['key'],
              'runner_sha256':sha(pathlib.Path(__file__)), 'harness_sha256':sha(ROOT/'tests/lifter_arithmetic_harness.c'),
              'limits':['not original-x86 differential execution', 'not actual Halo', 'upstream common 32-bit code/data offset window', 'no flags/floats/callback/thread/device coverage']}
    try:
        make_pe(work/'arithmetic.exe')
        (work/'global_aliases.sci').write_text('loc_401000,fixture_arithmetic\n')
        for name in ['relocations.csv','extern.llinc','macros.llinc']:
            (work/name).write_text('')
        def run(name, argv, timeout=45):
            with (evidence/(name+'.log')).open('x') as log:
                log.write('$ '+json.dumps([str(a) for a in argv])+'\n'); log.flush()
                subprocess.run([str(a) for a in argv],cwd=str(work),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=timeout)
        run('srw', [build/'SRW/SRW.exe','arithmetic.exe','arithmetic.llasm'])
        run('llasm', [build/'llasm/llasm','-m64','-ptrofs','-I',ROOT/'ref/sr/SR/llasm-support','-o','arithmetic.ll','arithmetic.llasm'])
        # HALOPAD_HOST_TARGET: an Intel Mac runs the fixture as x86_64 (the translation is the same IR)
        target = os.environ.get('HALOPAD_HOST_TARGET') or json.loads((ROOT/'toolchains.lock.json').read_text())['target']
        run('target', ['clang','-target',target,'-S','-emit-llvm','-x','c','/dev/null','-o','host-target.ll'])
        # Upstream emits generic LLVM with no target triple. Supply the actual compiler target.
        triple = next(l for l in (work/'host-target.ll').read_text().splitlines() if l.startswith('target triple ='))
        ir = work/'arithmetic.ll'
        ir.write_text(triple+'\n'+ir.read_text())
        run('compile', ['clang','-target',target,'-O2','-fno-fast-math','-ffp-contract=off','-Wall','-Wextra','-Werror','-I',ROOT/'ref/sr/SR/llasm-support',ROOT/'tests/lifter_arithmetic_harness.c',ir,'-o','native-smoke'])
        arch = subprocess.check_output(['lipo','-archs',str(work/'native-smoke')],text=True).strip()
        if arch != target.split('-')[0]:
            raise RuntimeError('fixture executable is not exclusively ' + target.split('-')[0])
        run('execute',[work/'native-smoke'])
        if 'PASS: 36 self-authored PE arithmetic cases' not in (evidence/'execute.log').read_text():
            raise RuntimeError('execution result missing expected case count')
        result.update(result='PASS',architecture=arch,artifacts={n:sha(work/n) for n in ['arithmetic.exe','arithmetic.ll','native-smoke']})
        print('PASS: 36 synthetic arithmetic cases; no Halo gate passed.')
        print('Evidence:',evidence.relative_to(ROOT))
    except Exception:
        result['result']='FAIL'
        raise
    finally:
        (evidence/'result.json').write_text(json.dumps(result,indent=2)+'\n')

if __name__ == '__main__':
    main()
