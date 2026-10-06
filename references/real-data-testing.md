# Testing a CWL tool on real data

Validation proves the file is well-formed. Only a run proves the command line is right.

## Contents
1. Choosing test data
2. Writing the job file
3. Running
4. Judging the result
5. Common run failures

## 1. Choosing test data

Real data from the tool's own sources beats anything synthetic: authors pick inputs that
exercise the tool, and some sets come with expected outputs. Look in this order
(`scripts/find_testdata.py PACKAGE --galaxy --nfcore [CLONE]` covers 1–4):

1. The tool's GitHub repository: `test/`, `tests/`, `example(s)/`, `data/`, `demo/`,
   `inst/extdata/` (R). samtools has hundreds of small files in `test/`.
2. Galaxy test data: `galaxyproject/tools-iuc/tools/<tool>/test-data` (bwa, seqtk, many
   more), and `galaxyproject/tools-devteam` for older tools (samtools). Files named like
   `*.out` or `*_result.*` are expected outputs: compare your output with them. Galaxy
   wrappers sometimes add a header line or reorder columns, so compare the data lines
   (`diff <(cat out.tsv) <(grep -v '^#' expected.out)`) before calling it a mismatch.
3. The bioconda recipe (`bioconda-recipes/recipes/<tool>/meta.yaml`): its `test: commands`
   show real invocations.
4. nf-core/test-datasets: its `modules` branch is a shared set of small real files that agree
   with each other (SARS-CoV-2 and human: genome FASTA + .fai/.dict/GFF/GTF, Illumina,
   nanopore and PacBio reads, BAM/CRAM, VCF, BED, 10x data, small kraken/pangolin
   databases); other branches hold per-pipeline data. With a local clone,
   `find_testdata.py PKG --nfcore /path/to/test-datasets --search 'REGEX'` searches every
   branch (`branch:path`) and `--get` extracts files with `git show`; without a clone,
   `--nfcore` lists the `modules` branch from GitHub.
5. The tool's documentation tutorials.
6. Only when none fits, the synthetic set below.

Record where each file came from (`find_testdata.py --get` writes `sources.json`).

- Synthetic fallback: `scripts/make_testdata.py testdata` writes
  one consistent set: `ref.fa` (+ `.fai`, `.dict`), paired `reads_1.fq`/`reads_2.fq`,
  `reads.sam`, `reads.bam` (+ `.bai`), `variants.vcf` (+ `.vcf.gz`, `.tbi`), `regions.bed`,
  `annotation.gff3`/`.gtf`, `protein.fa`, `table.tsv`, `text.txt`, and `manifest.json`.
  All coordinates agree with `ref.fa`, so tools that cross-check inputs accept them.
- Build any other format inside the tool's own image, so versions match, for example:
  - an aligner index: run the tool's own index command on `testdata/ref.fa`;
  - a CRAM: `samtools view -C -T ref.fa -o reads.cram reads.bam`;
  - a GATK dictionary when missing: `gatk CreateSequenceDictionary -R ref.fa`.
- Tools that need databases (kraken, busco lineages, Pfam): use the smallest official test
  database, or record the run as "needs database" instead of faking one.
- Put test data in a project `testdata/` folder and reuse it across tools.

## 2. Writing the job file

```yaml
reads:                 # File
  class: File
  path: testdata/reads.fq
vcfs:                  # File[]: always a YAML list
  - class: File
    path: testdata/a.g.vcf.gz
  - class: File
    path: testdata/b.g.vcf.gz
out_name: result.fa    # output path inputs are strings
threads: 2
```

- An array input must be a list. A map keyed by sample name (what R named lists or Python
  dicts produce) fails with `is not a list, expected list of File`.
- Paths are relative to the job file. The files must exist where cwltool runs; on a cluster,
  run cwltool on a node that mounts them.
- Index files (`.tbi`, `.bai`, `.fai`, `.dict`) must sit beside the primary file with the
  name the `secondaryFiles` pattern expects (`^.bai` = `x.bai`; `.bai` = `x.bam.bai`).
- Set only the inputs needed for a meaningful run; leave the rest to defaults.
- Output names decide index types: GATK writes `.tbi` for `x.vcf.gz` and `.idx` for `x.vcf`.
  Pick the name that matches the tool's `secondaryFiles`.

## 3. Running

```bash
python scripts/run_test.py TOOL.cwl JOB.yml --engine singularity   # HPC / no Docker
python scripts/run_test.py TOOL.cwl JOB.yml --engine docker
python scripts/run_test.py TOOL.cwl JOB.yml --lint-only            # job check only
```

It lints the job first, runs cwltool in `cwl-test-<tool>/`, keeps the log in
`cwl-test-<tool>/cwltool.log`, and checks each output. Use `--engine singularity` on nodes
without Docker; the DockerRequirement image is converted automatically.

For many tools, write one job file per tool in `tests/` and loop over them; keep a table of
tool, exit status and output check.

## 4. Judging the result

A run passes only when:
- cwltool exits 0,
- every required output exists and is non-empty, with its secondary files,
- the content is plausible: open it. FASTA from FASTQ starts with `>`, a BAM passes
  `samtools quickcheck`, a VCF has a header and records, a table has the expected columns.

Exit 0 with an empty output usually means the tool read stdin or wrote somewhere else.
Record the command line cwltool built (first lines of the log) next to the result.

## 5. Common run failures

| Symptom | Likely cause |
|---|---|
| exit 127, `command not found` | Wrong baseCommand: file-name command, interpreter + bare script, command not in image |
| `unrecognized option`, `invalid option` | Prefix wrong for this version: compare with help (dashes, spelling, `=`) |
| `Missing required secondary file` | Index not beside the input, or pattern wrong (`.idx` vs `.tbi`, `^.bai` vs `.bai`) |
| `Read-only file system` / permission denied | Tool writes next to a staged input; use `InitialWorkDirRequirement` with `writable: true` |
| Output glob matched nothing | Tool wrote another name: read the log and the tool's naming rule (prefix + suffix) |
| `No such file` for an output path | Output path typed File; make it string |
| Traceback in the tool | Bad argument value, missing database, or host packages leaking in (use `--singularity`, which isolates) |
| Killed / out of memory | Add `ResourceRequirement` (`ramMin`, `coresMin`) or use smaller data |
| Works with Docker, fails with Singularity | Image writes into its own filesystem or `$HOME`; Singularity images are read-only |
