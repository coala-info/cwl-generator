---
name: cwl-generator
description: Write, validate, and test CWL v1.2 CommandLineTools and Workflows, working directly as a coding agent. Use when given a container image (e.g. quay.io/biocontainers/samtools:1.23--h96c455f_0) or a bioconda package and asked to wrap its commands in CWL; when asked to build a CWL Workflow (pipeline) from tools, with scatter over samples, subworkflows, conditional steps or merged inputs; when testing or fixing existing CWL tools or workflows (wrong flags, baseCommand, types, outputs or wiring); when checking CWL against a tool's real help; or when running CWL on real test data with Docker or Singularity/Apptainer and judging the result. Covers command discovery in the image, safe help capture, writing tools from the help, workflow design and wiring, cwltool validation, error-pattern and wiring checks, finding real test data in the tool's source repository and Galaxy test sets, drafting job files, real-data runs, and job-file debugging.
---

# CWL generator

Tools (CommandLineTool) and pipelines (Workflow), CWL v1.2.

You write the CWL yourself from the tool's real help, then prove it runs. Every tool passes
four gates: **clean help → CWL written from that help → checks pass → real-data run passes**.
`cwltool --validate` alone proves little: most broken wrappers validate.

Bundled scripts (Python 3 + PyYAML; `cwltool` and Apptainer/Singularity or Docker on PATH):

| Script | Job |
|---|---|
| `scripts/container_help.py` | build the image once; list its commands; find subcommands; capture clean help |
| `scripts/check_cwl.py` | validate a tool; compare flags with the help both ways; flag known run-time errors |
| `scripts/check_workflow.py` | validate a workflow; check its wiring, types, requirements and index files (recurses into subworkflows) |
| `scripts/find_testdata.py` | find real test data in the tool's GitHub repo, bioconda recipe and Galaxy test sets; download chosen files |
| `scripts/make_testdata.py` | fallback: write a small consistent synthetic set (FASTA, FASTQ, SAM/BAM, VCF, BED, GFF, ...) |
| `scripts/make_job.py` | draft a job file for any CWL from a test-data folder (each pick explained, TODOs flagged) |
| `scripts/run_test.py` | lint the job file, run cwltool, check every output |

Run any script with `--help`. Work in one scratch folder (image cache, help files, tests).
Scripts live in this skill's folder; call them by full path.

## Workflow

### 1. Build the image once

```bash
python scripts/container_help.py build quay.io/biocontainers/seqtk:1.5--h577a1d6_1
```

Use an exact tag (bioconda: `quay.io/biocontainers/<pkg>:<version>--<build>`; look it up
on quay.io or the package's BioContainers page). Do not ask for help before the build
succeeds: a help call that triggers the conversion gets killed under its short timeout and
leaves GBs of temp files. On `auth token`/`toomanyrequests`, wait and retry.

### 2. Discover the commands

```bash
python scripts/container_help.py list IMAGE                 # the package's own commands
python scripts/container_help.py subcommands IMAGE seqtk    # when a command lists subcommands
```

- Wrap the package's tools only. `list` drops interpreters, system utilities, package
  managers and library helpers (`--all` shows them).
- One CWL file per command or subcommand: `seqtk_seq.cwl` runs `[seqtk, seq]`.
- Confirm every subcommand candidate with `help`. Help-sentence words (`The`, `Print`,
  `options`) are not subcommands.
- When there are many commands, ask the user which matter, or start with the main ones.

### 3. Capture the help

```bash
mkdir -p help && python scripts/container_help.py help IMAGE seqtk seq > help/seqtk_seq.txt
```

Exit 0: usable help. Exit 3: it lists subcommands (back to step 2). Exit 2: no usable help;
rerun with `--json` to see each attempt's verdict and read
[references/help-capture.md](references/help-capture.md). Never write a wrapper from bad
help (engine errors, tracebacks, "invalid option" only): that is how flags get invented.
If the help is thin, read the tool's manual or documentation and say which source you used.

### 4. Write the CWL

Read the help, build the input table, then write the file. Rules, type and binding tables,
output patterns, index files and a worked example: [references/cwl-authoring.md](references/cwl-authoring.md).
The rules that matter most:

- `baseCommand` lists the real command words: `[seqtk, seq]`. Never the CWL file name,
  never `[perl, script.pl]` with a bare script name (the interpreter does not search PATH;
  run the script itself or give its full path in the image).
- `hints: DockerRequirement: dockerPull: <exact image>`.
- One input per documented option. Each prefix is exactly the help's flag (dashes,
  spelling, `=`), without metavar or aliases. No `--help`/`--version` inputs.
- Usage-line order: `[...]` is optional, `<x>` is required and positional. Do not turn an
  optional `[-r in.bed]` into a positional input.
- Input files are `File`/`Directory` (with `secondaryFiles` for indexes). Output files, output
  prefixes and output directories are `string` inputs collected by an output `glob`.
- Collect every result the tool writes (stdout, named files, prefix files, directories).
- Map form for `inputs`/`outputs`, snake_case ids, `label` = file stem, `doc` from the help.

### 5. Check

```bash
python scripts/check_cwl.py seqtk_seq.cwl --help-file help/seqtk_seq.txt --package seqtk
```

Fix every `[ERROR]`, read every `[WARN]`, and run the check again until clean.
`help_flags_not_wrapped` lists documented flags with no input; `flag_not_in_help` lists
prefixes the help never mentions; `usage_positionals` means a required argument is missing
or optional. Each finding and its fix: [references/error-patterns.md](references/error-patterns.md).

### 6. Test on real data

```bash
python scripts/find_testdata.py seqtk --galaxy                        # list real test files
python scripts/find_testdata.py seqtk --galaxy --get 'seqtk_comp.fa' --out testdata
python scripts/make_job.py seqtk_comp.cwl --testdata testdata --out tests/seqtk_comp.yml
python scripts/run_test.py seqtk_comp.cwl tests/seqtk_comp.yml --engine singularity
```

Prefer real test data from the tool's own sources: the test/example folders of its GitHub
repository, Galaxy's curated test-data (which often includes the expected output: compare
with it), the bioconda recipe's test commands, and nf-core/test-datasets (`--nfcore [CLONE]
--search REGEX`; its `modules` branch has small consistent genome, reads, BAM and VCF sets). Use `make_testdata.py testdata` only when
none fits. Draft the job with `make_job.py`, fill its TODOs from the help (output names,
required values), and check every pick (arrays as YAML lists, output names as strings).
`run_test.py` lints the job, runs cwltool in `cwl-test-<tool>/`, and checks that every
output exists and is non-empty. Then open the output and confirm it makes sense
(FASTA starts with `>`, a BAM passes `samtools quickcheck`, a table has the expected columns).
Use `--engine docker` where Docker works; `singularity` on HPC nodes.

On failure, read the hint and the log, fix the CWL or the job, and repeat steps 5–6.
Details, building other formats, and a failure table:
[references/real-data-testing.md](references/real-data-testing.md).

A wrapper is done when one realistic run passes with plausible output. Also test each
important option group (an optional output, a paired-end mode) when the tool has one.

### 7. Report and clean up

Report one row per tool: command, help source, check result, test result (what was run and
what the output showed), and anything left undone with the reason. Then remove the cache:

```bash
python scripts/container_help.py clean
```

## Workflows

A Workflow wires tools together; build it only from tools that passed the four gates.

1. **Design.** List the steps (tool, inputs, outputs), what is shared (reference, index,
   database) and what varies per sample. Write or fix any missing tool first.
2. **Write** the workflow from [references/workflow-authoring.md](references/workflow-authoring.md):
   step `in` keys are the tool's input ids, sources are `input` or `step/output`, a scatter
   over samples usually runs a per-sample subworkflow, every scattered output gets a
   per-sample name via `valueFrom`, and workflow inputs that need index files declare
   `secondaryFiles` (else the runner never stages them).
3. **Check:** `python scripts/check_workflow.py pipeline.cwl` until no `[ERROR]`. It shows
   cwltool's warnings too ("Source ... may be incompatible with sink" means a type or
   secondaryFiles mismatch) and finds what cwltool lets through: misspelled step inputs,
   unconnected required inputs, lost index files, shards overwriting one output name.
4. **Test:** draft the job with `make_job.py` (then list every sample in each array, in the
   same order), run with `run_test.py`, and check each sample's outputs. Run each branch of a
   conditional step (`when` true and false); skipped outputs must come out `null`.
5. **Report** the per-step and per-sample results.

## Testing an existing CWL

Use the same gates on a CWL someone else wrote (no regeneration needed; for a Workflow,
use `check_workflow.py` in step 2):

1. Take the image from its `DockerRequirement` and the command from its `baseCommand`;
   `build` the image and capture `help` for that command.
2. `check_cwl.py TOOL.cwl --help-file ... --package ...`: a wrapper with errors will fail or
   mislead at run time; fix those first (or report them, if the file is not yours to change).
3. `find_testdata.py` for real inputs, `make_job.py` to draft the job, then fill the TODOs.
   Inputs whose `secondaryFiles` are computed by an expression need the files that
   expression names: `make_job.py` reads the extensions out of it.
4. `run_test.py`, then judge the content (and compare with expected outputs when the test set
   has them). Chain tools the way a workflow would: the index from `index` feeds `mem`.
5. Report per file: check findings, what ran, what the output showed, and any defect found.

## Many tools at once

- Build each image once and capture all its help before writing any CWL.
- At most 4 images in parallel (registry limits, disk space).
- Keep a results table (tool, gate reached, detail) and resume from it.
- When the list is long, prioritise by use (conda downloads, GitHub stars) and say so.
- Write into a scratch folder; copy into a repository only after checks and tests pass,
  keep a backup of anything replaced, and show the user before committing.
- Before committing, search the outputs for host paths (`/home/<user>`) and secrets.
