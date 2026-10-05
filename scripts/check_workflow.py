#!/usr/bin/env python3
"""Validate a CWL Workflow and check its wiring beyond what `cwltool --validate` reports.

  check_workflow.py WORKFLOW.cwl [--cwltool CMD]

1. `cwltool --validate`, printing its warnings too (type mismatches between a source and
   its sink are often only warnings: "Source ... may be incompatible").
2. Wiring checks, recursing into subworkflows:
   - a required input of a step's tool that the step neither connects nor defaults
   - a step `in` id the tool does not have (a typo is silently ignored by the runner)
   - an `out` id the tool does not produce
   - a source or outputSource that names no workflow input and no step output
   - index files lost at the workflow boundary: the tool input needs secondaryFiles, but
     the workflow input that feeds it declares none, so the runner never stages them
   - scattered steps whose output name is a tool default: every shard writes the same file
   - a `when` step whose outputs feed a non-optional sink without pickValue
   - workflow inputs no step uses; step outputs nothing uses
3. Shape checks: scatter of a non-array source, File fed into File[] (and back) without
   linkMerge/scatter, and features used without their requirement.

Exit 0 when valid and no ERROR finding.
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def items(section):
    """Steps in map form {id: step} or list form [{id: ..., ...}]."""
    if isinstance(section, dict):
        for k, v in section.items():
            yield str(k), v
    elif isinstance(section, list):
        for v in section:
            if isinstance(v, dict):
                yield str(v.get("id", "")).split("#")[-1].split("/")[-1], v


def requirement_classes(doc):
    names = set()
    for key in ("requirements", "hints"):
        r = doc.get(key) or []
        if isinstance(r, dict):
            names |= set(r)
        else:
            names |= {x.get("class") for x in r if isinstance(x, dict)}
    return names


def params(section):
    """Input/output parameters: map or list form; a bare type string becomes {type: ...}."""
    out = {}
    if isinstance(section, dict):
        for k, v in section.items():
            out[str(k)] = v if isinstance(v, dict) else {"type": v}
    elif isinstance(section, list):
        for v in section:
            if isinstance(v, dict):
                out[str(v.get("id", "")).split("#")[-1].split("/")[-1]] = v
    return out


def step_inputs(step):
    out = {}
    raw = step.get("in") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = v if isinstance(v, dict) else {"source": v}
    else:
        for v in raw:
            if isinstance(v, dict):
                out[str(v.get("id")).split("/")[-1]] = v
    return out


def step_outs(step):
    return [o if isinstance(o, str) else str(o.get("id")) for o in step.get("out") or []]


def type_info(t):
    """(base, optional, array_depth): File? -> (File, True, 0); File[] -> (File, False, 1)."""
    ts = t if isinstance(t, list) else [t]
    optional = any(x == "null" for x in ts)
    for x in ts:
        if x == "null":
            continue
        depth = 0
        while True:
            if isinstance(x, str):
                if x.endswith("?"):
                    optional, x = True, x[:-1]
                if x.endswith("[]"):
                    depth, x = depth + 1, x[:-2]
                    continue
                return x, optional, depth
            if isinstance(x, dict) and x.get("type") == "array":
                depth, x = depth + 1, x.get("items")
                continue
            if isinstance(x, list):          # union items: ["null", File] -> File, may be null
                optional = optional or "null" in x
                x = next((m for m in x if m != "null"), "null")
                continue
            if isinstance(x, dict):
                return str(x.get("type")), optional, depth
            return str(x), optional, depth
    return "null", True, 0


def sources(v):
    s = v.get("source")
    return [] if s is None else [s] if isinstance(s, str) else list(s)


def needs_index(spec):
    sf = spec.get("secondaryFiles")
    if not sf:
        return []
    sf = sf if isinstance(sf, list) else [sf]
    return [s.get("pattern") if isinstance(s, dict) else s for s in sf
            if not (isinstance(s, dict) and s.get("required") is False)]


class Checker:
    def __init__(self):
        self.f = []

    def add(self, level, where, msg):
        self.f.append((level, where, msg))

    def load_run(self, run, base):
        if isinstance(run, dict):
            return run, base
        path = (base / str(run).split("#")[0]).resolve()
        if not path.exists():
            return None, path
        try:
            return yaml.safe_load(path.read_text()), path.parent
        except yaml.YAMLError as e:
            self.add("WARN", path.name, f"PyYAML cannot parse it ({str(e).splitlines()[0]}); quote types "
                                        "like File? inside {...}; its wiring was not checked")
            return {}, path.parent

    def check(self, wf, base, where="workflow"):
        reqs = requirement_classes(wf) | getattr(self, "inherited", set())
        wf_in = params(wf.get("inputs"))
        wf_out = params(wf.get("outputs"))
        steps = {k: v for k, v in items(wf.get("steps"))}
        step_out_types, used_inputs, used_outputs, conditional = {}, set(), set(), {}
        runs = {}
        for sid, step in steps.items():
            tool, tbase = self.load_run(step.get("run"), base)
            runs[sid] = (tool, tbase)
            if tool is None:
                self.add("ERROR", f"{where}/{sid}", f"run file not found: {tbase}")
                continue
            if not tool:
                runs[sid] = (None, tbase)
                continue
            touts = params(tool.get("outputs"))
            scat = step.get("scatter")
            scat = [scat] if isinstance(scat, str) else list(scat or [])
            method = step.get("scatterMethod", "dotproduct")
            extra = len(scat) if (scat and method == "nested_crossproduct") else (1 if scat else 0)
            for o in step_outs(step):
                if o not in touts:
                    self.add("ERROR", f"{where}/{sid}", f"out '{o}' is not an output of {step.get('run')}")
                    continue
                b, opt, d = type_info(touts[o].get("type"))
                if b in ("stdout", "stderr"):
                    b = "File"
                step_out_types[f"{sid}/{o}"] = (b, opt or bool(step.get("when")), d + extra)
                conditional[f"{sid}/{o}"] = bool(step.get("when"))

        def source_type(src):
            src = src.lstrip("#").split("#")[-1]
            if src in wf_in:
                b, opt, d = type_info(wf_in[src].get("type"))
                return b, opt or "default" in wf_in[src], d, "input"
            if src in step_out_types:
                return (*step_out_types[src], "step")
            return None

        for sid, step in steps.items():
            tool, tbase = runs[sid]
            if tool is None:
                continue
            w = f"{where}/{sid}"
            if tool.get("class") == "Workflow":
                if "SubworkflowFeatureRequirement" not in reqs:
                    self.add("ERROR", w, "runs a Workflow: add SubworkflowFeatureRequirement")
                outer = getattr(self, "inherited", set())
                self.inherited = outer | reqs
                self.check(tool, tbase, w)
                self.inherited = outer
            tins = params(tool.get("inputs"))
            sins = step_inputs(step)
            scat = step.get("scatter")
            scat = {scat} if isinstance(scat, str) else set(scat or [])
            if scat and "ScatterFeatureRequirement" not in reqs:
                self.add("ERROR", w, "uses scatter: add ScatterFeatureRequirement")
            when = step.get("when")
            if when and "${" in str(when) and "InlineJavascriptRequirement" not in reqs:
                self.add("ERROR", w, "`when` uses a JavaScript body: add InlineJavascriptRequirement")
            for iid, spec in sins.items():
                srcs = sources(spec)
                if len(srcs) > 1 and "MultipleInputFeatureRequirement" not in reqs:
                    self.add("ERROR", w, f"in '{iid}' has several sources: add MultipleInputFeatureRequirement")
                if "valueFrom" in spec and "StepInputExpressionRequirement" not in reqs:
                    self.add("ERROR", w, f"in '{iid}' uses valueFrom: add StepInputExpressionRequirement")
                if iid not in tins and iid not in str(spec) and iid not in str(when) and \
                        not any(iid in str(x.get("valueFrom", "")) for x in sins.values()):
                    self.add("WARN", w, f"in '{iid}' is not an input of {step.get('run')} and no "
                                        "expression uses it (typo?)")
                for s in srcs:
                    st = source_type(s)
                    if st is None:
                        self.add("ERROR", w, f"in '{iid}': source '{s}' is no workflow input and no step output")
                        continue
                    if st[3] == "input":
                        used_inputs.add(s.lstrip("#"))
                        sink = tins.get(iid, {})
                        need = needs_index(sink)
                        if need and not needs_index(wf_in[s.lstrip('#')]):
                            # top level: the job file supplies the File, so nothing finds the index;
                            # in a subworkflow the File usually arrives from a step with them attached
                            self.add("ERROR" if where == "workflow" else "WARN", w,
                                     f"in '{iid}' needs secondaryFiles {need}, but workflow input "
                                                 f"'{s}' declares none, so they are never staged: copy the "
                                                 "secondaryFiles onto the workflow input")
                    else:
                        used_outputs.add(s)
                    if iid in tins and "valueFrom" not in spec:
                        tb, topt, td = type_info(tins[iid].get("type"))
                        sb, sopt, sd, _ = st
                        if iid in scat:
                            sd -= 1
                            if sd < 0:
                                self.add("ERROR", w, f"scatters over '{iid}', but its source '{s}' is not an array")
                        if len(srcs) > 1 or spec.get("linkMerge") or spec.get("pickValue"):
                            continue
                        if sb != "Any" and tb != "Any" and sb != tb:
                            self.add("ERROR", w, f"in '{iid}': source '{s}' is {sb}{'[]' * max(sd, 0)}, "
                                                 f"the tool wants {tb}{'[]' * td}")
                        elif sd != td and sd >= 0:
                            self.add("ERROR", w, f"in '{iid}': source '{s}' has array depth {sd}, the tool "
                                                 f"wants {td} (scatter, or linkMerge/merge_flattened?)")
                        if sopt and not topt and "default" not in spec and "default" not in tins[iid]:
                            why = ("comes from a conditional step (when)" if conditional.get(s.lstrip("#"))
                                   else "is optional")
                            self.add("WARN", w, f"in '{iid}': source '{s}' {why} but the tool input is required")
            for tid, tspec in tins.items():
                tb, topt, td = type_info(tspec.get("type"))
                if not topt and "default" not in tspec and tid not in sins:
                    self.add("ERROR", w, f"required tool input '{tid}' is not connected and has no default")
            if scat:
                for o in step_outs(step):
                    glob = str((params(tool.get("outputs")).get(o, {}).get("outputBinding") or {}).get("glob", ""))
                    for ref in re.findall(r"inputs\.(\w+)", glob):
                        if ref not in sins and "default" in tins.get(ref, {}):
                            self.add("WARN", w, f"scattered, but output '{o}' is named by the tool default of "
                                                f"'{ref}': every shard writes the same file; set '{ref}' per "
                                                "shard with valueFrom")
                    if not glob and str(params(tool.get("outputs")).get(o, {}).get("type")) in ("stdout",) \
                            and "$(" not in str(tool.get("stdout", "")):
                        self.add("WARN", w, f"scattered, and stdout name '{tool.get('stdout')}' is fixed")
        for oid, spec in wf_out.items():
            srcs = spec.get("outputSource")
            srcs = [] if srcs is None else [srcs] if isinstance(srcs, str) else list(srcs)
            if not srcs:
                self.add("ERROR", f"{where} output {oid}", "no outputSource")
            if len(srcs) > 1 and "MultipleInputFeatureRequirement" not in reqs:
                self.add("ERROR", f"{where} output {oid}", "several outputSources: add MultipleInputFeatureRequirement")
            for s in srcs:
                st = source_type(s)
                if st is None:
                    self.add("ERROR", f"{where} output {oid}", f"outputSource '{s}' does not exist")
                    continue
                used_outputs.add(s.lstrip("#"))
                ob, oopt, od = type_info(spec.get("type"))
                sb, sopt, sd, kind = st
                if len(srcs) == 1 and not spec.get("linkMerge") and not spec.get("pickValue"):
                    if sb != ob and "Any" not in (sb, ob):
                        self.add("ERROR", f"{where} output {oid}", f"type {ob}{'[]' * od} but '{s}' is {sb}{'[]' * sd}")
                    elif sd != od:
                        self.add("ERROR", f"{where} output {oid}", f"array depth {od} but '{s}' has {sd} "
                                                                   "(a scattered step returns one array level per scatter)")
                    if sopt and not oopt:
                        self.add("WARN", f"{where} output {oid}", f"'{s}' can be null (conditional or optional) "
                                                                  "but the output type is not optional")
        for i in wf_in:
            if i not in used_inputs and not any(i in str(s.get("when", "")) for s in steps.values()):
                self.add("WARN", where, f"workflow input '{i}' is not used by any step")
        for so in step_out_types:
            if so not in used_outputs:
                self.add("WARN", where, f"step output '{so}' is not used and not a workflow output")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflow")
    ap.add_argument("--cwltool", default="cwltool")
    a = ap.parse_args()
    path = Path(a.workflow).resolve()
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run(a.cwltool.split() + ["--validate", str(path)], capture_output=True, text=True, cwd=d)
    msgs = [re.sub(r"\x1b\[[0-9;]*m", "", l) for l in (p.stdout + p.stderr).splitlines()]
    keep = [l for l in msgs if l.strip() and not re.match(r"^(INFO|\s*$)", l)
            and "is valid CWL" not in l]
    print(f"[validate] {'PASS' if p.returncode == 0 else 'FAIL'} {path.name}")
    # cwltool wraps each message over many indented lines: join them, one message per line
    joined = re.sub(r"\s+", " ", " ".join(keep))
    for m in re.split(r"(?=\b(?:WARNING|ERROR) )", joined):
        m = re.sub(r"\S*/(?=[\w.-]+\.cwl:)", "", m.strip())     # keep file names, drop long paths
        if m:
            print("    [cwltool] " + m[:400])
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        sys.exit(f"PyYAML cannot parse {path.name}: {str(e).splitlines()[0]} (quote types like File? inside {{...}})")
    if not isinstance(doc, dict) or doc.get("class") != "Workflow":
        sys.exit("not a Workflow (use check_cwl.py for a CommandLineTool)")
    c = Checker()
    c.check(doc, path.parent)
    seen = set()
    for level, where, msg in c.f:
        if (level, where, msg) not in seen:
            seen.add((level, where, msg))
            print(f"[{level}] {where}: {msg}")
    errors = sum(1 for l, _, _ in seen if l == "ERROR")
    print(f"[summary] valid={p.returncode == 0} errors={errors} warnings={sum(1 for l, _, _ in seen if l == 'WARN')}")
    sys.exit(0 if p.returncode == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
