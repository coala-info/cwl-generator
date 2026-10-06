#!/usr/bin/env python3
"""Validate a generated CWL CommandLineTool and check it against the tool's own help.

  check_cwl.py TOOL.cwl [--help-file HELP.txt] [--package PKG] [--cwltool CMD]

1. `cwltool --validate` (run from a scratch directory: a folder named after a Python package,
   e.g. ./simplejson, would otherwise shadow that package and break cwltool).
2. Checks for error patterns that pass validation but fail at run time, and a comparison
   of the CWL's flags with the help in both directions (see references/error-patterns.md).
   Each finding says what to change; edit the file and run the check again.

Exit 0: valid and no ERROR finding. Exit 1: invalid, or an ERROR finding.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

INTERPRETERS = {"perl", "python", "python2", "python3", "Rscript", "R", "bash", "sh", "ruby", "node"}
SYSTEM_UTILITIES = set("""ls cat less more head tail grep sed awk sort uniq cut tr wc find xargs du df
chmod chown ln rm cp mv mkdir touch man top ps ip ifconfig traceroute which env echo tar gzip
gunzip zcat curl wget date file stat id diff seq join split paste tac test timeout""".split())
OUTPUT_DOC_RE = re.compile(r"(?i)^\W*(?:the\s+)?(?:path\s+(?:to|of)\s+(?:the\s+)?)?(?:output|out)\b"
                           r"(?:\s+\w+){0,3}\s+(?:file|path|name|filename|prefix)\b"
                           r"|\b(?:write|save|store)\s+(?:the\s+)?(?:output|results?)\s+(?:to|in)\b")
INPUT_DOC_RE = re.compile(r"(?i)\b(?:input|existing|previous|from\s+a|generated\s+by|read|load)\b")


def items(section):
    if isinstance(section, dict):
        for k, v in section.items():
            yield k, (v if isinstance(v, dict) else {"type": v})
    elif isinstance(section, list):
        for v in section:
            if isinstance(v, dict):
                yield v.get("id"), v


def types_of(spec):
    t = spec.get("type")
    ts = t if isinstance(t, list) else [t]
    out = []
    for x in ts:
        if isinstance(x, str):
            out.append(x.rstrip("?"))
        elif isinstance(x, dict) and x.get("type") == "array":
            out.append(f"{x.get('items')}[]")
    return [x for x in out if x != "null"]


def validate(cwltool, path):
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run(cwltool.split() + ["--validate", str(Path(path).resolve())],
                           capture_output=True, text=True, cwd=d)
    msg = [l for l in (p.stdout + p.stderr).splitlines() if l.strip() and "INFO" not in l[:12]]
    return p.returncode == 0, msg[-6:]


def checks(doc, help_text, package, stem=None):
    """(level, code, message) findings; levels ERROR (will fail at run time) and WARN."""
    f = []
    bc = doc.get("baseCommand")
    words = bc if isinstance(bc, list) else [bc] if bc else []
    first = str(words[0]) if words else ""
    if not words and not doc.get("arguments"):
        f.append(("ERROR", "no_base_command", "no baseCommand"))
    if isinstance(bc, str) and " " in bc.strip():
        f.append(("ERROR", "base_command_one_string",
                  f"baseCommand {bc!r} is one argv word; write it as a list {bc.split()}"))
    if (len(words) >= 2 and first.rsplit("/", 1)[-1] in INTERPRETERS and "/" not in str(words[1])
            and re.search(r"\.(pl|py|R|r|sh|rb|js)$", str(words[1]))):
        f.append(("ERROR", "interpreter_bare_script",
                  f"{words[:2]}: the interpreter looks for {words[1]} in the empty working directory; "
                  "run the installed script directly, or give its full path in the image"))
    if len(words) == 1 and first.rsplit("/", 1)[-1] in INTERPRETERS:
        f.append(("ERROR", "runs_interpreter", f"baseCommand is the bare interpreter {first!r}"))
    # only when the command IS the file name (samtools_sort.cwl -> `samtools_sort`);
    # real programs can start with the package name too (agat_convert_sp_gff2bed.pl)
    # ... unless the captured help shows the program exists (art_454, art_SOLiD in the art image)
    if package and first.startswith(package + "_") and (stem is None or first in (stem, doc.get("label"))) \
            and not (help_text and re.search(r"(?i)\busage\b", help_text)):
        f.append(("ERROR", "base_command_is_file_name",
                  f"baseCommand {first!r} is the CWL file name, not a program "
                  f"(`{package} {first[len(package) + 1:]}`, or `{first[len(package) + 1:]}`?)"))
    if package and first.rsplit("/", 1)[-1] in SYSTEM_UTILITIES and package not in SYSTEM_UTILITIES:
        f.append(("WARN", "runs_system_utility",
                  f"baseCommand {first!r} is a system utility, not {package} (lost package word?)"))

    ins = list(items(doc.get("inputs")))
    ids = Counter(i for i, _ in ins)
    for i, n in ids.items():
        if n > 1:
            f.append(("ERROR", "duplicate_input_id",
                      f"input id {i!r} appears {n} times: every copy gets the same value"))
    prefixes = Counter()
    for iid, s in ins:
        b = s.get("inputBinding") if isinstance(s.get("inputBinding"), dict) else {}
        pre = b.get("prefix")
        ts = types_of(s)
        t = s.get("type")                                  # prefix on each array item counts too
        for x in (t if isinstance(t, list) else [t]):
            if isinstance(x, dict) and isinstance(x.get("inputBinding"), dict) \
                    and isinstance(x["inputBinding"].get("prefix"), str):
                prefixes[x["inputBinding"]["prefix"]] += 1
        doc_text = " ".join(str(s.get("doc") or "").split())
        if isinstance(pre, str):
            prefixes[pre] += 1
            if " " in pre.strip():
                f.append(("ERROR", "prefix_has_space",
                          f"{iid}: prefix {pre!r} is passed as ONE argument; keep only the flag"))
            # `-name+` is a real bedtools flag: a spec only when the help does not print it as is
            if re.search(r"(!|\+|=[sif]|:[sif])$|^-{1,2}\w+\|\w", pre) and not (
                    help_text and re.search(r"(?<![\w-])" + re.escape(pre) + r"(?!\S)", help_text)):
                f.append(("ERROR", "perl_getopt_spec",
                          f"{iid}: prefix {pre!r} is a Perl Getopt spec, not a flag"))
            # `--version <pkg version>` is a real option in some tools (anchore-cli, ariba getref)
            if pre.strip() in ("--help", "-help") or (
                    pre.strip() in ("-h", "-v", "-V", "--version") and ts == ["boolean"]
                    and re.search(r"(?i)\b(help|version|usage)\b", doc_text)):
                f.append(("WARN", "meta_option_input", f"{iid}: {pre} only prints help/version"))
            if help_text and " " not in pre.strip():
                bare = pre.rstrip("=")
                if not re.search(r"(?<![\w-])" + re.escape(bare) + r"(?![\w-])", help_text):
                    f.append(("WARN", "flag_not_in_help",
                              f"{iid}: {pre} is not in the help (invented, or wrong dash count/spelling?)"))
            if pre.endswith("=") and b.get("separate", True) is not False:
                f.append(("ERROR", "equals_prefix_separate",
                          f"{iid}: prefix {pre!r} needs separate: false (else `{pre} value`)"))
        if ts in (["File"],) and OUTPUT_DOC_RE.search(doc_text) and not INPUT_DOC_RE.search(doc_text):
            f.append(("ERROR", "output_path_is_File",
                      f"{iid}: looks like an output path but is File (must exist before the run); "
                      "make it string and collect the file with an output glob"))
        # "Output directory" / "Path to the output folder", not "BiG-SCAPE output directory" or
        # "output directory from baktfold predict" (another tool's output, read here)
        if ts == ["Directory"] and re.search(
                r"(?i)^\W*(?:the\s+)?(?:path\s+(?:to|of)\s+(?:the\s+)?)?(?:output|out|results?)\s+(dir|directory|folder)\b"
                r"|\b(?:write|save|store)\s+(?:\w+\s+){0,3}(?:to|in|into)\s+(?:this\s+|the\s+)?(dir|directory|folder)\b",
                doc_text) and not INPUT_DOC_RE.search(doc_text) and not re.search(r"(?i)\bfrom\s+\w", doc_text):
            f.append(("ERROR", "output_dir_is_Directory",
                      f"{iid}: output directory typed Directory (staged read-only); use string"))
        if any(t.endswith("[]") for t in ts) and isinstance(s.get("inputBinding"), dict) \
                and pre and not b.get("itemSeparator"):
            item_b = None
            t = s.get("type")
            for x in (t if isinstance(t, list) else [t]):
                if isinstance(x, dict) and isinstance(x.get("inputBinding"), dict):
                    item_b = x["inputBinding"]
            # argparse nargs (`--tracks TRACKS [TRACKS ...]`, `--in A B ...`): one flag, many values
            # (`--tracks [t1 ...]`, `--dimensions px px`)
            q = re.escape(pre)
            nargs = help_text and re.search(q + r"[ =]\S+ \[\S+ \.\.\.\]|" + q + r"[ =]\S+ (?:\S+ )?\.\.\.|"
                                            + q + r"[ =]\[\S+ \.\.\.\]|" + q + r"[ =](\S+) \1(?!\S)", help_text)
            if not item_b and not nargs:
                f.append(("WARN", "array_prefix_once",
                          f"{iid}: array with prefix {pre} renders `{pre} a b c`; if the tool wants "
                          f"`{pre} a {pre} b`, put the prefix on the items' inputBinding"))
    if help_text:
        # flags the help documents that no input binds: a usage-only help (`[-r in.bed]`)
        # is easily turned into a bare positional, and the flag is lost
        documented = set(re.findall(r"(?:^|[\s\[(|,])(-{1,2}[A-Za-z][\w-]*)", help_text))
        bound = {p.rstrip("=") for p in prefixes} | {str(x) for x in (doc.get("arguments") or [])}
        meta = {"-h", "--help", "-help", "--version", "-V", "--usage"}
        # aliases printed together (`-O, --output-fmt FORMAT`, `-@, --threads INT`) are one option
        for line in help_text.splitlines():
            head = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            names = re.findall(r"(?:^|[\s,|/])(-{1,2}[A-Za-z@][\w-]*)", " " + head)
            if len(names) > 1 and bound & set(names):
                bound |= set(names)
        def covered(flag):
            if flag.endswith("-"):                        # `--initial-` cut at a line wrap, `--kallisto-fastx-*`
                return True
            if re.match(r"^-[A-Za-z]\d", flag):          # -q2, -k14: a value glued in an example
                return flag[:2] in bound
            if re.match(r"^-[A-Za-z]{2,}$", flag):        # -mu, -TdBOELU: bundled single-letter switches
                return all(f"-{c}" in bound for c in flag[1:])
            return False
        missing = sorted(x for x in documented - bound - meta if not covered(x))
        if missing:
            f.append(("WARN", "help_flags_not_wrapped",
                      f"the help documents {missing[:12]}{' ...' if len(missing) > 12 else ''} but no "
                      "input binds them; check positional inputs that should carry one of these flags"))
    if help_text:
        usage = re.search(r"(?im)^\s*usage:?\s*(.+(?:\n[ \t]{6,}.+)*)", help_text)
        if usage:
            line = re.sub(r"^\s*\S+", " ", usage.group(1), count=1)  # the program name (`ASTRAL`) is no slot
            line = re.sub(r"\[[^\[\]]*\]", " ", line)   # drop optional [ ... ] parts
            line = re.sub(r"\[[^\[\]]*\]", " ", line)
            line = re.sub(r"(<[^<>]+>)(?:\s*\|\s*<[^<>]+>)+", r"\1", line)  # <in.fq>|<in.fa> is one slot
            # `(-i|--input) <input file>`: alternatives of one flag, then its value
            line = re.sub(r"\((-{1,2}[A-Za-z][\w-]*(?:\|-{1,2}[A-Za-z][\w-]*)*)\)\s*(<[^<>]+>|[A-Z][A-Z0-9_]+)",
                          lambda m: m.group(1).split("|")[0], line)
            # a metavar after a flag (-i INPUT, --out=<file>) is the flag's value, not a positional
            line = re.sub(r"(?<![\w-])(-{1,2}[A-Za-z][\w-]*)[ =](<[^<>]+>|[A-Z][A-Z0-9_]+)", r"\1", line)
            required_slots = re.findall(r"<[^<>]+>|(?<![\w-])[A-Z][A-Z0-9_]{2,}(?![\w-])", line)
            required_slots = [x for x in required_slots if x not in ("OPTIONS", "OPTION", "COMMAND", "ARGS")
                              and not x.startswith("<-")]   # clap `<--variant <V>|--all>`: one of these flags
            positional_required = [
                iid for iid, s in ins
                if isinstance(s.get("inputBinding"), dict) and not s["inputBinding"].get("prefix")
                and ("default" in s       # a positional with a default is always passed
                     or ("null" not in (s.get("type") if isinstance(s.get("type"), list) else [s.get("type")])
                         and not str(s.get("type")).endswith("?")))]
            # an output name built in `arguments` (no prefix) also fills a usage slot
            positional_required += [f"arguments[{i}]" for i, a in enumerate(doc.get("arguments") or [])
                                    if isinstance(a, dict) and "prefix" not in a and "position" in a]
            if len(required_slots) > len(positional_required):
                f.append(("WARN", "usage_positionals",
                          f"the usage line requires {required_slots} but the CWL has "
                          f"{len(positional_required)} required positional input(s) {positional_required}: "
                          "a required argument is optional or missing"))
    for pre, n in prefixes.items():
        if n > 1 and pre:
            f.append(("WARN", "shared_prefix", f"prefix {pre} is bound by {n} inputs"))
    in_ids = {i for i, _ in ins}
    for oid, o in items(doc.get("outputs")):
        ob = o.get("outputBinding") if isinstance(o.get("outputBinding"), dict) else {}
        glob = ob.get("glob")
        gl = json.dumps(glob)
        if glob == "*.out" and not str(doc.get("stdout") or "").endswith(".out"):
            f.append(("ERROR", "placeholder_glob", f"output {oid} globs '*.out', which nothing writes"))
        for ref in re.findall(r"inputs\.(\w+)", gl):
            if ref not in in_ids:
                f.append(("ERROR", "glob_undefined_input", f"output {oid} uses inputs.{ref}, which is not an input"))
        if not glob and o.get("type") not in ("stdout", "stderr") and "outputEval" not in ob:
            f.append(("WARN", "output_without_glob", f"output {oid} has no glob"))
    return f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cwl")
    ap.add_argument("--help-file", help="the tool's help text (from container_help.py help)")
    ap.add_argument("--package", help="package name; enables file-name and system-utility checks")
    ap.add_argument("--cwltool", default="cwltool", help="cwltool command (default: cwltool on PATH)")
    a = ap.parse_args()
    path = Path(a.cwl)
    text = path.read_text()
    help_text = Path(a.help_file).read_text(errors="replace") if a.help_file else ""
    ok, msg = validate(a.cwltool, path)
    print(f"[validate] {'PASS' if ok else 'FAIL'} {path}")
    for m in ([] if ok else msg):
        print("    " + m)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print(f"[yaml] cannot parse: {e}")
        sys.exit(1)
    findings = checks(doc, help_text, a.package, path.name[:-4]) if isinstance(doc, dict) else []
    for level, code, m in findings:
        print(f"[{level}] {code}: {m}")
    errors = sum(1 for l, _, _ in findings if l == "ERROR")
    print(f"[summary] valid={ok} errors={errors} warnings={sum(1 for l, _, _ in findings if l == 'WARN')}")
    sys.exit(0 if ok and errors == 0 else 1)


if __name__ == "__main__":
    main()
