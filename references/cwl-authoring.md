# Writing a CWL CommandLineTool from help text

## Contents
1. Template
2. From help to an input table
3. Types
4. Bindings
5. Outputs
6. Requirements
7. Index files (secondaryFiles)
8. Naming
9. Worked example

## 1. Template

```yaml
cwlVersion: v1.2
class: CommandLineTool
label: samtools_sort
doc: Sort alignments by leftmost coordinates, or by read name.
baseCommand: [samtools, sort]
hints:
  DockerRequirement:
    dockerPull: quay.io/biocontainers/samtools:1.23--h96c455f_0
  SoftwareRequirement:
    packages: {samtools: {version: ["1.23"]}}
inputs: {}
outputs: {}
```

Use the map form (`inputs: {id: {...}}`) for new files: an id can then appear only once.
Put the image under `hints` so users can run without a container; use `requirements` only
when the tool truly cannot run elsewhere.

## 2. From help to an input table

Before writing YAML, list every option the help documents:

| id | flag(s) | takes value? | metavar / default | required? | input or output? |
|---|---|---|---|---|---|

- Take every flag exactly as printed. When the help lists aliases (`-o, --output FILE`),
  use one (prefer the long form; a short-only help means short).
- A metavar after the flag (`-o FILE`, `--threads INT`, `-q=N`) means it takes a value;
  no metavar means a switch.
- `[...]` in the usage line means optional; `<x>` or bare UPPERCASE outside brackets means
  required. Required options become non-optional inputs.
- Decide input vs output from the doc text: "output", "write to", "save", "prefix for output
  files" mean the tool creates it. That decides the type (section 3).
- Skip `-h/--help`, `--version`, `--citation`, interactive or GUI options. Keep `--verbose`
  and threads.
- Do not invent options from prose, examples, or other tools. If the help is short, wrap
  what it shows; a later reader can add more from the manual.

## 3. Types

| Help says | CWL type |
|---|---|
| switch | `boolean?` (binding writes only the prefix when true) |
| INT, NUM, N | `int` (`long` for genome sizes) |
| FLOAT, FRAC, RATE | `float` |
| STR, NAME, TAG, choices `{a,b,c}` | `string` (or `enum` when the set is fixed and short) |
| input FILE, `<in.bam>` | `File` |
| input DIR (database, index folder) | `Directory` |
| output file, output prefix | `string` (the tool creates it; a File must exist beforehand) |
| output or temp directory | `string` (Directory inputs are read-only) |
| repeatable option, several files | array: `File[]`, `string[]` |
| optional anything | append `?`: `int?`, `File?`, `File[]?` |

Give a `default` only when the tool needs a value the user would always pass (an output
name such as `sorted.bam`). Do not copy every help default into CWL; the tool applies them.

## 4. Bindings

| Help form | Binding |
|---|---|
| positional `<in.fa>` | `inputBinding: {position: 10}` (positions in usage order; options use a lower number such as 1) |
| `-t INT` | `{prefix: -t}` |
| switch `-u` | `boolean?` with `{prefix: -u}` |
| `--out=FILE` (help shows `=`) | `{prefix: "--out=", separate: false}` |
| `-q20` (glued) | `{prefix: -q, separate: false}` |
| `key=value` (BBTools `in=x`) | `{prefix: "in=", separate: false}` |
| `--opt[=VAL]` (optional value) | `{prefix: "--opt=", separate: false}` |
| `-I file -I file` (repeat flag) | `type: {type: array, items: File, inputBinding: {prefix: -I}}` and `inputBinding: {position: 1}` on the input |
| `-I a,b,c` | `type: File[]`, `inputBinding: {prefix: -I, itemSeparator: ","}` |
| `-i a b c` (one flag, many values) | `type: File[]`, `inputBinding: {prefix: -i}` |
| `--[no]copy` | two booleans, prefixes `--copy` and `--nocopy` |
| fixed arguments | `arguments: [{prefix: --format, valueFrom: tsv, position: 1}]` |

Quote prefixes and patterns YAML could misread: `'-1'`, `'-2'`, `'.0123'`, `"in="`.

## 5. Outputs

| Tool behaviour | Output |
|---|---|
| prints results to stdout | `out: {type: stdout}` and top-level `stdout: $(inputs.<main>.nameroot).txt` (or a fixed name) |
| writes the path given by `-o NAME` | input `output_name: string` (default e.g. `out.bam`); output `{type: File, outputBinding: {glob: $(inputs.output_name)}}` |
| writes `PREFIX.*` | input `prefix: string`; output `{type: File[], outputBinding: {glob: "$(inputs.prefix)*"}}`, or one output per known suffix |
| writes into `-d DIR` | input `outdir: string` (default `out`); output `{type: Directory, outputBinding: {glob: $(inputs.outdir)}}` |
| writes a fixed name in the working dir | `glob: fixed_name.txt` |
| writes next to its input (index, in-place edit) | `InitialWorkDirRequirement` with `listing: [{entry: $(inputs.bam), writable: true}]`, then glob the basename |
| also writes an index | `secondaryFiles` on the output (`.bai`, `.tbi`, `.csi`) |
| log on stderr worth keeping | `log: {type: stderr}` and `stderr: tool.log` |

Make an output optional (`File?`) when the tool writes it only for some options.
Never use a placeholder glob such as `*.out` that nothing writes.

## 6. Requirements

- `InlineJavascriptRequirement`: only when an expression uses JavaScript (`${...}` or
  method calls). Parameter references like `$(inputs.x.nameroot)` do not need it.
- `InitialWorkDirRequirement`: stage inputs writable, or create a config file the tool reads:
  `listing: [{entryname: config.ini, entry: "key=$(inputs.value)\n"}]`.
- `ResourceRequirement`: `coresMin`/`ramMin` (MiB) for heavy tools; bind threads to
  `$(runtime.cores)` with `arguments: [{prefix: -t, valueFrom: $(runtime.cores)}]`.
- `EnvVarRequirement`: tools configured by environment variables (`JAVA_OPTS`, `TMPDIR`).
- `NetworkAccess`: tools that download (database fetchers).
- Avoid `ShellCommandRequirement` and pipes; use one tool per CWL file.

## 7. Index files (secondaryFiles)

| Primary | Pattern |
|---|---|
| BAM | `.bai` (x.bam.bai) or `^.bai` (x.bai); accept both: `[{pattern: .bai, required: false}, {pattern: ^.bai, required: false}]` |
| CRAM | `.crai` |
| VCF.GZ / BED.GZ (tabix) | `.tbi` (or `.csi`) |
| VCF written by GATK as `.vcf` | `.idx` |
| FASTA reference | `.fai`; GATK also `^.dict` |
| BWA index | `.amb`, `.ann`, `.bwt`, `.pac`, `.sa` |
| Bowtie2 index | pass the index prefix as a Directory plus a string basename instead |

## 8. Naming

- File: `<package>.cwl` for a single-command package, `<package>_<subcommand>.cwl`
  for subcommands (`samtools_sort.cwl`). The file name never goes into `baseCommand`.
- Input ids: snake_case from the long flag (`--min-length` → `min_length`); for short-only
  flags, from the doc (`-q INT  mask quality below INT` → `mask_quality`). Unique.
- `label`: the file stem; `doc`: the help's first sentence. Keep each input's `doc` short,
  from the help.

## 9. Worked example

Help (`seqtk comp`):

```
Usage:  seqtk comp [-u] [-r in.bed] <in.fa>
Output format: chr, length, #A, #C, #G, #T, #2, #3, #4, #CpG, #tv, #ts, #CpG-ts
```

```yaml
cwlVersion: v1.2
class: CommandLineTool
label: seqtk_comp
doc: Report the nucleotide composition of FASTA/Q sequences.
baseCommand: [seqtk, comp]
hints:
  DockerRequirement:
    dockerPull: quay.io/biocontainers/seqtk:1.5--h577a1d6_1
inputs:
  upper_only:
    type: boolean?
    doc: Count uppercase bases only.
    inputBinding: {prefix: -u, position: 1}
  regions:
    type: File?
    doc: BED file; report composition inside these regions.
    inputBinding: {prefix: -r, position: 1}
  sequences:
    type: File
    doc: Input FASTA or FASTQ.
    inputBinding: {position: 10}
outputs:
  composition:
    type: stdout
    doc: Tab-separated composition table.
stdout: $(inputs.sequences.nameroot).comp.tsv
```

Note what the usage line gives: `-u` is a switch, `-r` takes a file, `<in.fa>` is required
and positional. A generator that turns `[-r in.bed]` into a second positional loses `-r`.
