#!/usr/bin/env python3
"""Draft a job file for an existing CWL tool from a folder of test data.

  make_job.py TOOL.cwl --testdata DIR [--out JOB.yml] [--all]

For each input (required ones; with --all also optional File inputs) it picks a value:
  File/File[]  a test file whose kind matches the input (EDAM format, secondaryFiles,
               extension or words in the id/doc: reference, reads, bam, vcf, bed, ...),
               preferring files whose index files exist beside them
  Directory    a sub-folder of DIR when one name matches, else TODO
  enum         the first symbol
  string/int/float/boolean without a default: TODO (fill by hand from the help)

Every guess is printed with its reason. Read the job before running it: a wrong pick is
cheaper to fix here than in a failed run. Paths are written relative to the job file.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import yaml

# kind -> (file extensions, words in id/doc, EDAM format ids)
KINDS = {
    "fasta": ({".fa", ".fasta", ".fna", ".fas"}, r"fasta|reference|genome|ref\b|contig|assembl|target|idxbase|prefix",
              {"edam:format_1929", "format_1929"}),
    "fastq": ({".fq", ".fastq"}, r"fastq|reads?\b|read[12]|mates?|r[12]\b|fq\b|query|in[12]?_fq", {"format_1930"}),
    "bam": ({".bam"}, r"\bbam\b|alignment|aligned|mapped", {"format_2572"}),
    "sam": ({".sam"}, r"\bsam\b", {"format_2573"}),
    "cram": ({".cram"}, r"cram", {"format_3462"}),
    "vcf": ({".vcf", ".vcf.gz", ".bcf"}, r"vcf|variant|bcf|calls", {"format_3016"}),
    "bed": ({".bed"}, r"\bbed\b|region|interval|target", {"format_3003"}),
    "gff": ({".gff", ".gff3", ".gtf"}, r"gff|gtf|annotation|gene", {"format_1975", "format_2306"}),
    "protein": ({".faa"}, r"protein|peptide|aa\b", set()),
    "table": ({".tsv", ".csv", ".txt"}, r"table|tsv|csv|matrix|metadata|list|text", set()),
    "sai": ({".sai"}, r"\bsai\b", set()),
}
COMPRESS = re.compile(r"\.(gz|bgz|bz2|xz)$")


def items(section):
    if isinstance(section, dict):
        for k, v in section.items():
            yield k, (v if isinstance(v, dict) else {"type": v})
    elif isinstance(section, list):
        for v in section:
            if isinstance(v, dict):
                yield str(v.get("id")).split("#")[-1], v


def shape(t):
    """(base type, optional, is_array, enum symbols)"""
    ts = t if isinstance(t, list) else [t]
    optional = any(x == "null" for x in ts)
    for x in ts:
        if x == "null":
            continue
        if isinstance(x, str):
            optional |= x.endswith("?")
            base = x.rstrip("?")
            if base.endswith("[]"):
                return base[:-2], optional, True, None
            return base, optional, False, None
        if isinstance(x, dict):
            if x.get("type") == "array":
                return str(x.get("items")), optional, True, None
            if x.get("type") == "enum":
                return "enum", optional, False, [str(s).split("/")[-1] for s in x.get("symbols", [])]
    return "unknown", optional, False, None


def ext_of(p):
    name = COMPRESS.sub("", p.name.lower())
    return Path(name).suffix


def kind_of(path):
    e = ext_of(path)
    if path.name.lower().endswith((".vcf.gz", ".vcf.bgz")):
        return "vcf"
    for k, (exts, _, _) in KINDS.items():
        if e in exts:
            return k
    return None


def secondary_ok(path, spec):
    for sf in spec.get("secondaryFiles") or []:
        pat = sf.get("pattern") if isinstance(sf, dict) else sf
        if (isinstance(sf, dict) and sf.get("required") is False) or not isinstance(pat, str) or "$(" in pat:
            continue
        name = path.name
        while pat.startswith("^"):
            pat, name = pat[1:], name.rsplit(".", 1)[0]
        if not (path.parent / (name + pat)).exists():
            return False
    return True


def wanted_kinds(iid, spec):
    text = f"{iid} {spec.get('doc') or ''} {spec.get('label') or ''}".lower()
    fmt = str(spec.get("format") or "")
    found = []
    for k, (_, words, edam) in KINDS.items():
        if any(e in fmt for e in edam):
            found.insert(0, (k, f"format {fmt}"))
        elif re.search(words, iid.lower()):
            found.append((k, f"id '{iid}'"))
    sec = " ".join(str(s.get("pattern") if isinstance(s, dict) else s) for s in spec.get("secondaryFiles") or [])
    if re.search(r"\.amb|\.bwt|\.fai|\.dict", sec):
        found.insert(0, ("fasta", "secondaryFiles look like a reference index"))
    if re.search(r"\.bai|\.csi", sec):
        found.insert(0, ("bam", "secondaryFiles .bai/.csi"))
    if re.search(r"\.tbi", sec):
        found.insert(0, ("vcf", "secondaryFiles .tbi"))
    for k, (_, words, _) in KINDS.items():
        if re.search(words, text) and k not in [f[0] for f in found]:
            found.append((k, "doc text"))
    return found


def expression_extensions(spec):
    """Extensions named inside secondaryFiles expressions (`['.amb', '.ann', '.pac']`), and the
    one the expression strips from the primary (`basename.replace(/\\.bwt$/, '')` -> .bwt)."""
    exts, stripped = set(), None
    for sf in spec.get("secondaryFiles") or []:
        pat = sf.get("pattern") if isinstance(sf, dict) else sf
        if isinstance(pat, str) and ("$(" in pat or "${" in pat):
            exts |= set(re.findall(r"['\"](\.[A-Za-z0-9]+)['\"]", pat))
            m = re.search(r"replace\(\s*/\\\.(\w+)\$/", pat)
            stripped = stripped or (m and "." + m.group(1))
    return exts, stripped


def has_siblings(f, exts):
    stem = f.name[:-len(f.suffix)] if f.suffix else f.name
    return all((f.parent / (stem + e)).exists() or (f.parent / (f.name + e)).exists()
               for e in exts if e != f.suffix)


def pick(iid, spec, files, used):
    exts, stripped = expression_extensions(spec)
    if exts:
        # the CWL computes its index file names: pick a file whose named siblings all exist;
        # the primary is the file with the extension the expression strips (mt.bwt), or else
        # a file the siblings are named after (ref.fa for ref.fa.amb)
        cands = [f for f in files if has_siblings(f, exts)]
        cands.sort(key=lambda f: (f.suffix != stripped if stripped else f.suffix in exts,
                                  f in used, f.stat().st_size))
        if cands:
            return cands[0], f"siblings {sorted(exts)} exist (from the secondaryFiles expression)"
    for kind, why in wanted_kinds(iid, spec):
        cands = [f for f in files if kind_of(f) == kind]
        cands.sort(key=lambda f: (not secondary_ok(f, spec), f in used, f.stat().st_size))
        if cands:
            f = cands[0]
            note = "" if secondary_ok(f, spec) else " (WARNING: its secondaryFiles are missing)"
            return f, f"{kind} by {why}{note}"
    return None, "no matching test file"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cwl")
    ap.add_argument("--testdata", required=True)
    ap.add_argument("--out", help="job file to write (default: print)")
    ap.add_argument("--all", action="store_true", help="also fill optional File inputs")
    a = ap.parse_args()
    tool = yaml.safe_load(Path(a.cwl).read_text())
    td = Path(a.testdata).resolve()
    files = sorted(p for p in td.rglob("*") if p.is_file() and not p.name.endswith((".json", ".md")))
    job_dir = Path(a.out).resolve().parent if a.out else Path.cwd()
    job, notes, todo, used = {}, [], [], []
    for iid, spec in items(tool.get("inputs")):
        base, optional, is_array, symbols = shape(spec.get("type"))
        has_default = "default" in spec
        if base in ("File", "Directory") and (not optional or a.all) and not has_default:
            if base == "Directory":
                dirs = [d for d in td.rglob("*") if d.is_dir() and re.search(iid.split("_")[0], d.name, re.I)]
                if dirs:
                    job[iid] = {"class": "Directory", "path": os.path.relpath(dirs[0], job_dir)}
                    notes.append(f"{iid}: folder {dirs[0].name}")
                else:
                    todo.append(f"{iid}: Directory")
                continue
            f, why = pick(iid, spec, files, used)
            if not f:
                (todo if not optional else notes).append(f"{iid}: File - {why}")
                continue
            used.append(f)
            value = {"class": "File", "path": os.path.relpath(f, job_dir)}
            job[iid] = [value] if is_array else value
            notes.append(f"{iid}: {f.name} ({why})")
        elif not optional and not has_default:
            if base == "enum" and symbols:
                job[iid] = symbols[0]
                notes.append(f"{iid}: first enum symbol {symbols[0]!r}")
            else:
                job[iid] = "TODO"
                todo.append(f"{iid}: required {base}{'[]' if is_array else ''} with no default")
    text = yaml.safe_dump(job, sort_keys=False)
    if a.out:
        Path(a.out).write_text(text)
        print(f"[job] wrote {a.out}")
    else:
        print(text, end="")
    for n in notes:
        print(f"[pick] {n}", file=sys.stderr)
    for t in todo:
        print(f"[TODO] {t}", file=sys.stderr)
    sys.exit(2 if todo else 0)


if __name__ == "__main__":
    main()
