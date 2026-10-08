#!/usr/bin/env python3
"""Translate the Xbox engine's arm64_32 guest image into base-relative ARM64.

Upstream's Android build links the game as one static arm64_32 image (64-bit
AArch64 instructions, 32-bit pointers) at guest address 0x88000000. Apple
platforms keep the low 4 GiB of every process unmapped, so that code cannot
run where it was linked. This tool rewrites every instruction of the image's
.text into ordinary ARM64 assembly for the host:

- guest memory is one 4 GiB-aligned host reservation; x28 holds its base,
  so a guest address g lives at x28 + g. Because the base is 4 GiB-aligned,
  the low 32 bits of a host address inside it are the guest address;
- every load and store through a general register becomes
  "add x27, x28, wN, uxtw" and the access through x27 (write-back forms copy
  the new address back into wN);
- the stack pointer is a real host address inside guest memory, so accesses
  through sp stay as they are; an instruction that copies sp into a general
  register is followed by "mov wD, wD", so guest code only ever holds guest
  addresses;
- adrp/adr become constants (the guest address they would have produced);
- direct branches go to the translated instruction's label; blr/br go
  through _xg_dispatch, which maps a guest code address to its translation;
- calls to the import stubs (host_*, hostgl_*, hostposix_*) become direct
  calls to the host's implementation _xh_<name>.

The guest must be compiled with -ffixed-x27 -ffixed-x28 -fno-jump-tables
(scripts/xbox/guest-cc.sh); the tool checks that neither register is used.

Usage: translate.py <halo_guest.elf> <out.s> [--llvm-bin DIR]
"""
import argparse
import os
import re
import subprocess
import sys

MEM = re.compile(r"\[(x\d+|sp)((?:, [^\]]*)?)\](!?)")
ADDR = re.compile(r"0x([0-9a-f]+)(?: <[^>]*>)?")
SYM_HEADER = re.compile(r"^([0-9a-f]+) <(.+)>:$")
LINE = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)(?:\s+(.*))?$")

BRANCH_LABEL = {"b", "bl", "cbz", "cbnz"}
INVERT_TB = {"tbz": "tbnz", "tbnz": "tbz"}
SP_READERS = {"add", "sub", "mov", "and", "orr", "adds", "subs"}


def lab(address):
    return "L_%x" % address


def movc(reg32, value):
    value &= 0xFFFFFFFF
    out = ["\tmovz\t%s, #0x%x" % (reg32, value & 0xFFFF)]
    if value >> 16:
        out.append("\tmovk\t%s, #0x%x, lsl #16" % (reg32, value >> 16))
    return out


def w(reg):
    return "w" + reg[1:] if reg.startswith("x") else reg


def symbols(elf, llvm):
    out = subprocess.run([llvm + "/llvm-nm", "--defined-only", elf], check=True,
                         capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            table.setdefault(parts[2], int(parts[0], 16))
    return table


def section(elf, llvm, name):
    out = subprocess.run([llvm + "/llvm-readelf", "-S", "-W", elf], check=True,
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        fields = line.replace("[", " ").replace("]", " ").split()
        if name in fields:
            i = fields.index(name)
            return int(fields[i + 2], 16), int(fields[i + 4], 16)
    raise SystemExit("no section " + name)


class Translator:
    def __init__(self, imports):
        self.imports = imports  # guest address -> import name
        self.out = []
        self.unhandled = {}

    def emit(self, *lines):
        self.out.extend(lines)

    def target(self, text):
        # the branch target is the last address (tbz's bit number comes first)
        match = list(ADDR.finditer(text))[-1]
        return int(match.group(1), 16), match

    def instruction(self, address, op, args):
        args = (args or "").split("//")[0].strip()
        if op == "nop":
            return
        if op in ("adrp", "adr"):
            dest, rest = args.split(", ", 1)
            value, _ = self.target(rest)
            self.emit(*movc(w(dest), value))
            return
        if op in BRANCH_LABEL or op.startswith("b."):
            value, match = self.target(args)
            name = self.imports.get(value)
            if name and op in ("b", "bl"):
                self.emit("\t%s\t_xh_%s" % (op, name))
            else:
                self.emit("\t%s\t%s" % (op, args[:match.start()] + lab(value)))
            return
        if op in INVERT_TB:
            value, match = self.target(args)
            skip = "L_%x_s" % address
            self.emit("\t%s\t%s%s" % (INVERT_TB[op], args[:match.start()], skip),
                      "\tb\t" + lab(value), skip + ":")
            return
        if op in ("blr", "br"):
            reg = args.strip()
            if reg == "x30":
                self.emit("\t%s\tx30" % op)
            else:
                self.emit("\tmov\tw27, %s" % w(reg),
                          "\t%s\t_xg_dispatch" % ("bl" if op == "blr" else "b"))
            return
        if op == "ret":
            self.emit("\tret" if args in ("", "x30") else "\tret\t" + args)
            return
        if op in ("ldr", "ldrsw", "prfm") and "[" not in args and "0x" in args:
            raise SystemExit("literal load at %x is not supported yet" % address)
        match = MEM.search(args)
        if match:
            base, rest, bang = match.group(1), match.group(2), match.group(3)
            if base == "sp":
                self.emit("\t%s\t%s" % (op, args))
                return
            post = args[match.end():].startswith(",")
            new = args[:match.start()] + "[x27%s]%s" % (rest, bang) + args[match.end():]
            self.emit("\tadd\tx27, x28, %s, uxtw" % w(base), "\t%s\t%s" % (op, new))
            if bang or post:
                self.emit("\tmov\t%s, w27" % w(base))
            return
        operands = [part.strip() for part in args.split(",")] if args else []
        if operands and op in SP_READERS:
            dest = operands[0]
            if dest == "sp" and "sp" not in operands[1:]:
                source = operands[1]
                rest = args.split(",", 2)[2] if len(operands) > 2 else None
                self.emit("\tadd\tx27, x28, %s, uxtw" % w(source),
                          "\t%s\tsp, x27%s" % (op, "," + rest if rest else ""))
                return
            if dest != "sp" and "sp" in operands[1:]:
                self.emit("\t%s\t%s" % (op, args), "\tmov\t%s, %s" % (w(dest), w(dest)))
                return
        if re.search(r"\b[xw]2[78]\b", args):
            raise SystemExit("guest uses a reserved register at %x: %s %s" % (address, op, args))
        self.emit("\t%s\t%s" % (op, args) if args else "\t" + op)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("elf")
    parser.add_argument("out")
    parser.add_argument("--llvm-bin", default=os.environ.get("XBOX_LLVM_BIN", "/opt/homebrew/opt/llvm/bin"))
    parser.add_argument("--imports", nargs="*", default=[],
                        help="files listing the import stub names")
    options = parser.parse_args()

    syms = symbols(options.elf, options.llvm_bin)
    names = []
    for path in options.imports:
        for line in open(path):
            line = line.split("#")[0].strip()
            if line:
                names.append(line)
    imports = {syms[n]: n for n in names if n in syms}
    missing = [n for n in names if n not in syms]
    if missing:
        print("note: %d listed imports are not in the image" % len(missing), file=sys.stderr)
    text_start, text_size = section(options.elf, options.llvm_bin, ".text")
    text_end = text_start + text_size

    dis = subprocess.Popen([options.llvm_bin + "/llvm-objdump", "-d", "--no-show-raw-insn",
                            "--section=.text", options.elf], stdout=subprocess.PIPE, text=True)
    t = Translator(imports)
    seen = []
    for raw in dis.stdout:
        line = raw.rstrip("\n")
        match = LINE.match(line)
        if not match:
            continue
        address = int(match.group(1), 16)
        if not (text_start <= address < text_end):
            continue
        seen.append(address)
        t.emit(lab(address) + ":")
        if address in imports:
            t.emit("\tb\t_xh_" + imports[address])
            continue
        t.instruction(address, match.group(2), match.group(3))
    if dis.wait():
        raise SystemExit("llvm-objdump failed")

    with open(options.out, "w") as out:
        out.write("// generated by scripts/xbox/translate.py; do not edit or share\n")
        out.write("\t.text\n\t.p2align 2\n\t.globl _xg_text\n_xg_text:\n")
        out.write("\n".join(t.out))
        out.write("\nL_trap:\n\tmov\tw0, w27\n\tb\t_xg_bad_transfer\n")
        # _xg_dispatch: w27 = guest code address; branches to its translation
        # (x30 is left as the caller set it, so blr and br both work)
        out.write("\t.globl _xg_dispatch\n\t.p2align 2\n_xg_dispatch:\n")
        out.write("\n".join(movc("w16", text_start)) + "\n")
        out.write("\tsub\tw16, w27, w16\n")
        out.write("\n".join(movc("w17", text_size)) + "\n")
        out.write("\tcmp\tw16, w17\n\tb.hs\tL_trap\n\ttst\tw16, #3\n\tb.ne\tL_trap\n")
        out.write("\tadrp\tx17, _xg_table@PAGE\n\tadd\tx17, x17, _xg_table@PAGEOFF\n")
        out.write("\tldrsw\tx16, [x17, w16, uxtw]\n\tadd\tx16, x17, x16\n\tbr\tx16\n")
        # dispatch: one 32-bit offset (from _xg_table) per 4-byte guest slot
        out.write("\t.section __TEXT,__const\n\t.p2align 2\n\t.globl _xg_table\n_xg_table:\n")
        valid = set(seen)
        for slot in range(text_start, text_end, 4):
            out.write("\t.long\t%s - _xg_table\n" % (lab(slot) if slot in valid else "L_trap"))
        out.write("\t.globl _xg_text_start\n\t.set _xg_text_start, 0x%x\n" % text_start)
        out.write("\t.globl _xg_text_slots\n\t.set _xg_text_slots, %d\n" % (text_size // 4))
    print("translated %d instructions, %d imports" % (len(seen), len(imports)))


if __name__ == "__main__":
    main()
