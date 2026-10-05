# cwl-generator

A skill that lets a coding agent (Claude Code, or any agent that reads skills) write,
validate and test [CWL v1.2](https://www.commonwl.org/v1.2/) tools and workflows.

The agent does the work itself. It reads the tool's real help from its container, writes
the CWL, checks it, and runs it on real data. No code generator or API key is involved.

## What it does

- **Wrap the commands in a container image as CWL CommandLineTools.** It finds the
  commands, captures clean help, writes the CWL from that help, and checks it.
- **Build CWL Workflows from tools.** This covers scatter over samples, subworkflows,
  conditional steps (`when`) and merged inputs.
- **Test existing CWL files**, written by anyone, on real data.
- **Find real test data** in the tool's own GitHub repository, in Galaxy's test data, and in
  the bioconda recipe.

Every tool passes four gates: **clean help → CWL written from that help → checks pass →
real-data run passes**. `cwltool --validate` alone is not enough: most broken wrappers
still validate.

## Requirements

- Python 3 with PyYAML
- `cwltool` on PATH
- Apptainer/Singularity or Docker
- Network access to quay.io (images), and to GitHub and anaconda.org (test data)
- Optional: `GITHUB_TOKEN` in the environment. GitHub allows 60 API requests an hour
  without it, which is about 15–20 packages.

## Install

Copy the folder into a skills folder your agent reads:

```bash
cp -r cwl-generator ~/.claude/skills/           # all your projects
```

```bash
cp -r cwl-generator <project>/.claude/skills/   # one project
```

In Claude Code, run it with `/cwl-generator`. The agent also picks it up by itself when you
ask for CWL work.

## Example requests

```text
/cwl-generator generate cwls for bwa: quay.io/biocontainers/bwa:0.7.19--h577a1d6_1
/cwl-generator build a workflow: bwa index, then bwa mem and samtools sort for each sample
/cwl-generator test data/bwa/bwa_mem.cwl on real data
```

## Folder contents

```text
cwl-generator/
├── SKILL.md                     instructions the agent follows
├── scripts/
│   ├── container_help.py        build image once; list commands; find subcommands; capture help
│   ├── check_cwl.py             validate a tool; compare its flags with the help; known errors
│   ├── check_workflow.py        validate a workflow; check wiring, types, requirements, index files
│   ├── find_testdata.py         find and download real test data (GitHub, Galaxy, bioconda)
│   ├── make_testdata.py         fallback: small synthetic FASTA/FASTQ/SAM/BAM/VCF/BED/GFF set
│   ├── make_job.py              draft a job file for any CWL from a test-data folder
│   └── run_test.py              check the job, run cwltool, check every output
└── references/
    ├── cwl-authoring.md         turning help text into a CommandLineTool
    ├── workflow-authoring.md    writing Workflows: wiring, patterns, requirements, example
    ├── help-capture.md          getting clean help out of a container
    ├── error-patterns.md        errors that validate but fail at run time, with fixes
    └── real-data-testing.md     test data sources, job files, judging results
```

The scripts also work without an agent. Each has `--help`. A typical tool run:

```bash
S=cwl-generator/scripts
IMG=quay.io/biocontainers/seqtk:1.5--h577a1d6_1
python $S/container_help.py build $IMG                        # pull and convert once
python $S/container_help.py list $IMG                         # the package's commands
python $S/container_help.py help $IMG seqtk comp > help/seqtk_comp.txt
# ... write seqtk_comp.cwl from the help ...
python $S/check_cwl.py seqtk_comp.cwl --help-file help/seqtk_comp.txt --package seqtk
python $S/find_testdata.py seqtk --galaxy --get 'seqtk_comp.fa' 'seqtk_comp.out' --out testdata
python $S/make_job.py seqtk_comp.cwl --testdata testdata --out tests/seqtk_comp.yml
python $S/run_test.py seqtk_comp.cwl tests/seqtk_comp.yml --engine singularity
python $S/container_help.py clean                             # remove the image cache
```

For a workflow, use `check_workflow.py pipeline.cwl` in place of `check_cwl.py`.

## How it keeps runs safe and clean

- **Images are built once, with a long timeout.** A short help call that triggers the
  image conversion gets killed partway, and Apptainer then leaves a multi-GB
  `/tmp/build-temp-*` folder behind.
- **Cache and temp files stay in the working folder** (`.cwl-image-cache`).
  `container_help.py clean` removes them.
- **Containers run with a clean environment and an empty home folder** (`/home/user`).
  Your own `~/.local` Python packages cannot leak into the tool, and your home path
  cannot end up in recorded help.
- **Nothing is downloaded without `--get`.** Downloaded test data has its source recorded
  in `sources.json`.

## What was tested

- **bwa 0.7.19:** 15 tools written from help, all run on real data. Every read aligned to its
  true position. The step-by-step index files are byte-identical to the ones `bwa index`
  makes.
- **samtools sort:** written from help, then run. The output is a sorted, indexed BAM that
  passes `samtools quickcheck`.
- **seqtk comp:** the output matches Galaxy's expected output line for line.
- **Existing CWL files** (coala-repo `bwa_index`, `bwa_mem`) on Galaxy's MiSeq data:
  340 of 341 reads mapped.
- **Workflow `bwa_align`:** index once, then a per-sample subworkflow (mem → sort, plus a
  conditional XA expansion), run on two samples. Each sample gets its own read group and an
  indexed BAM. Skipped conditional outputs come out `null`.

## Limits

- **Tool wrappers wrap documented options only.** If the help is thin, the agent reads the
  manual or source and says so. A package with no command-line tool (a library) gets no
  wrapper.
- **Job values need a person or the agent to fill them.** `make_job.py` picks files but
  cannot guess values only the help defines, such as output names. It marks them TODO. For
  workflows it puts one file in each array, so you list every sample yourself.
- **The checks catch known patterns, not every mistake.** A real-data run is the final gate.
  Judge the output content, not only that it exists.
- **Some tools need a database** (kraken, busco lineages). Use the smallest official test
  database, or record the run as "needs database".
