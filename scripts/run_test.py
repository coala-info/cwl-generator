#!/usr/bin/env python3
"""Run a CWL tool (or workflow) on real data and check what it produced.

  run_test.py TOOL.cwl JOB.yml [--engine singularity|docker|none] [--outdir DIR] [--timeout S]

1. Lint the job against the tool's inputs before running: required inputs present, arrays
   given as YAML lists (not maps keyed by sample name), File values exist locally, and
   simple secondaryFiles (.tbi, .fai, ^.bai) exist beside their primary file.
2. Run cwltool in OUTDIR (default ./cwl-test-<tool>), log to OUTDIR/cwltool.log.
3. Check every declared output: present, non-empty, secondary files present.
   On failure, print the log tail with a hint for known error patterns.

Exit 0 only when the run succeeds and every required output exists and is non-empty.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

HINTS = [
    (r"is not a list, expected list of", "an array input was given as a map; write it as a YAML list (`- class: File`)"),
    (r"Missing required secondary file|secondary file .* does not exist|Did not find required secondary",
     "a secondaryFiles pattern does not match a real index file (e.g. .idx vs .tbi, ^.bai vs .bam.bai)"),
    (r"exited with status 127|command not found|executable file not found",
     "the baseCommand is not on PATH in the image (file-name command? interpreter + bare script?)"),
    (r"exited with status 126|Permission denied", "not executable, or the tool writes into a read-only staged input"),
    (r"Read-only file system", "the tool writes next to its input; stage it writable (InitialWorkDirRequirement writable: true)"),
    (r"unrecognized option|invalid option|unknown option|no such option|Unknown argument|unrecognized arguments",
     "a flag is wrong for this tool version: compare the prefix with the help"),
    (r"Traceback \(most recent call last\)", "the tool crashed: read the traceback for the bad argument or input"),
    (r"No such file or directory", "a path is wrong: an output path typed File, or a relative path not staged"),
    (r"did not find output|No such file.*glob|output .* not found|Output .* is not optional",
     "an output glob matches nothing: check the glob against the files the tool wrote"),
    (r"Docker is not available|docker: command not found|Cannot connect to the Docker daemon",
     "no Docker on this node: rerun with --engine singularity"),
    (r"no space left on device", "disk full (container cache or tmp); clean caches and rerun"),
    (r"Killed|out of memory|MemoryError", "out of memory: add ResourceRequirement ramMin or use a smaller test"),
]


def items(section):
    if isinstance(section, dict):
        for k, v in section.items():
            yield k, (v if isinstance(v, dict) else {"type": v})
    elif isinstance(section, list):
        for v in section:
            if isinstance(v, dict):
                yield str(v.get("id", "")).split("#")[-1], v


def type_info(t):
    ts = t if isinstance(t, list) else [t]
    optional = any(x == "null" or (isinstance(x, str) and x.endswith("?")) for x in ts)
    kinds = []
    for x in ts:
        if isinstance(x, str) and x != "null":
            base = x.rstrip("?")
            kinds.append("array" if base.endswith("[]") else base)
        elif isinstance(x, dict):
            kinds.append(x.get("type"))
    return optional, kinds


def file_path(v, job_dir):
    p = v.get("path") or v.get("location") or ""
    p = re.sub(r"^file://", "", p)
    return Path(p) if os.path.isabs(p) else (job_dir / p)


def lint_job(tool, job, job_dir):
    problems, warnings = [], []
    for iid, spec in items(tool.get("inputs")):
        optional, kinds = type_info(spec.get("type"))
        val = job.get(iid)
        if val is None:
            if not optional and "default" not in spec:
                problems.append(f"{iid}: required input missing from the job")
            continue
        if "array" in kinds and not isinstance(val, list):
            how = "a map keyed by name" if isinstance(val, dict) else type(val).__name__
            problems.append(f"{iid}: is an array input but the job gives {how}; write a YAML list "
                            f"(`{iid}:` then `- class: File` / `  path: ...` per item)")
            continue
        for v in (val if isinstance(val, list) else [val]):
            if isinstance(v, dict) and v.get("class") in ("File", "Directory"):
                p = file_path(v, job_dir)
                if not p.exists():
                    warnings.append(f"{iid}: {p} does not exist on this machine (fine only if cwltool "
                                    "runs where it does)")
                    continue
                for sf in spec.get("secondaryFiles") or []:
                    pat = sf.get("pattern") if isinstance(sf, dict) else sf
                    req = not (isinstance(sf, dict) and sf.get("required") is False)
                    if not isinstance(pat, str) or "$(" in pat or "${" in pat or pat.endswith("?"):
                        continue
                    name = p.name
                    while pat.startswith("^"):
                        pat = pat[1:]
                        name = name.rsplit(".", 1)[0]
                    if req and not (p.parent / (name + pat)).exists():
                        problems.append(f"{iid}: secondary file {name + pat} missing beside {p.name}")
    known = {i for i, _ in items(tool.get("inputs"))}
    for k in job:
        if k not in known:
            warnings.append(f"{k}: in the job but not an input of the tool (typo?)")
    return problems, warnings


def check_outputs(tool, result):
    rows, bad = [], 0
    for oid, spec in items(tool.get("outputs")):
        optional, _ = type_info(spec.get("type"))
        val = result.get(oid)
        if val is None:
            rows.append((oid, "MISSING" if not optional else "absent (optional)", ""))
            bad += 0 if optional else 1
            continue
        for v in (val if isinstance(val, list) else [val]):
            if not isinstance(v, dict) or v.get("class") not in ("File", "Directory"):
                rows.append((oid, "value", json.dumps(v)[:60]))
                continue
            p = Path(re.sub(r"^file://", "", v.get("location", v.get("path", ""))))
            size = v.get("size", p.stat().st_size if p.is_file() else 0)
            status = "ok" if (v["class"] == "Directory" or size > 0) else "EMPTY"
            bad += status == "EMPTY" and not optional
            sec = [Path(re.sub(r"^file://", "", s.get("location", ""))).name for s in v.get("secondaryFiles", [])]
            rows.append((oid, status, f"{p.name} {size} B" + (f" + {', '.join(sec)}" if sec else "")))
    return rows, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cwl")
    ap.add_argument("job")
    ap.add_argument("--engine", default="singularity", choices=["singularity", "docker", "none"])
    ap.add_argument("--outdir")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--cwltool", default="cwltool")
    ap.add_argument("--lint-only", action="store_true")
    a = ap.parse_args()
    cwl, job_path = Path(a.cwl).resolve(), Path(a.job).resolve()
    tool = yaml.safe_load(cwl.read_text())
    job = yaml.safe_load(job_path.read_text()) or {}
    problems, warnings = lint_job(tool, job, job_path.parent)
    for w in warnings:
        print(f"[job WARN] {w}")
    for p in problems:
        print(f"[job ERROR] {p}")
    if problems or a.lint_only:
        sys.exit(1 if problems else 0)

    out = Path(a.outdir or f"cwl-test-{cwl.stem}").resolve()
    (out / "tmp").mkdir(parents=True, exist_ok=True)
    cmd = a.cwltool.split() + ["--outdir", str(out / "results"), "--tmpdir-prefix", str(out / "tmp") + "/"]
    cmd += {"singularity": ["--singularity"], "docker": [], "none": ["--no-container"]}[a.engine]
    cmd += [str(cwl), str(job_path)]
    print("[run] " + " ".join(cmd))
    env = dict(os.environ)
    if a.engine == "singularity":  # keep image cache and build temp out of /tmp and ~
        env.setdefault("CWL_SINGULARITY_CACHE", str(out.parent / ".cwl-image-cache"))
        Path(env["CWL_SINGULARITY_CACHE"]).mkdir(parents=True, exist_ok=True)
        for k in ("APPTAINER_TMPDIR", "SINGULARITY_TMPDIR"):
            env.setdefault(k, str(out / "tmp"))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout, cwd=out, env=env)
    except subprocess.TimeoutExpired:
        print(f"[run] TIMEOUT after {a.timeout}s")
        sys.exit(1)
    (out / "cwltool.log").write_text(p.stderr)
    try:
        result = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        result = {}
    if p.returncode != 0:
        print(f"[run] FAILED (exit {p.returncode}); log: {out / 'cwltool.log'}")
        tail = [l for l in p.stderr.splitlines() if l.strip()][-25:]
        print("\n".join("    " + l for l in tail))
        for rx, hint in HINTS:
            if re.search(rx, p.stderr, re.I):
                print(f"[hint] {hint}")
        sys.exit(1)
    rows, bad = check_outputs(tool, result)
    print(f"[run] success; outputs in {out / 'results'}")
    for oid, status, detail in rows:
        print(f"    {oid:28s} {status:18s} {detail}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
