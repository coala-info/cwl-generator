# Error patterns in generated CWL tools

Seen across many thousands of generated CommandLineTools. Each one passes `cwltool --validate`
and still fails, or does the wrong thing, at run time. `scripts/check_cwl.py` detects the ones
marked (auto); the rest need a look at the help. Check your own files against this list too.

## Contents
1. Command (baseCommand)
2. Flags (prefix and binding)
3. Input types
4. Outputs
5. Help-text problems that poison generation
6. Junk files to delete, not fix

## 1. Command (baseCommand)

| Pattern | Example | Fix |
|---|---|---|
| Command words in one string (auto) | `baseCommand: "samtools view"` | List: `[samtools, view]` |
| Interpreter plus bare script (auto) | `[perl, EDTA.pl]` | The interpreter looks in the empty working directory, not PATH. Run `EDTA.pl` directly (bioconda scripts are on PATH with a shebang), or give the full path in the image (`Rscript /files/x/wrapper.R`). Confirm with `container_help.py list`. |
| Bare interpreter (auto) | `baseCommand: java`, `perl` | The help recorded is the interpreter's. Find the real entry point (wrapper script, `java -jar /path/x.jar`). |
| CWL file name used as the command (auto) | `seq2c_lr2gene.pl`, `sambamba_subsample` | `lr2gene.pl`; `sambamba subsample`. But `humann_renorm_table` IS the real program name: check `list` before splitting. |
| Package word lost (auto, warn) | `samtools_sort` runs `sort` | `[samtools, sort]`. A help retry that drops the program name captures GNU `sort` help. |
| Subcommand never run | `bindash_The.cwl` runs `bindash` | The "subcommand" was a word from a help sentence. Delete if another file wraps `bindash`; otherwise rename to `bindash.cwl`. |
| Package name split | `[var, pubs, deploy-db]` | `[varpubs, deploy-db]` |
| Version string as command | `SVDB-2.8.4` | `svdb` |

## 2. Flags (prefix and binding)

| Pattern | Example | Fix |
|---|---|---|
| Metavar or alias list in the prefix (auto) | `-g GENEPATH`, `--quirk [noStar]`, `--force-rooted, --rooted` | Keep only the flag: `-g`, `--quirk`, `--force-rooted` |
| Perl Getopt spec as prefix (auto) | `--amosfile!`, `--dbdir=s`, `--verbose+`, `-s\|sdmopt` | `--amosfile`, `--dbdir`, `--verbose` (boolean), `-s` |
| Flag not in the help (auto, warn) | `--kmer-subsampling` | Use the help's spelling even if it is a typo (`--kmer-subampling`). Also: underscores mixed into kebab-case, invented long forms over a short option (`--database` for `-db`). |
| Wrong dash count | `--num_threads` for BLAST+ | Use the one form the help documents (`-num_threads`). |
| `key=value` options | BBTools `in=`, GATK2, biobambam | `prefix: "in="`, `separate: false` |
| Prefix ending in `=` but separate (auto) | `--ratio=` + value as next word | `separate: false` |
| Optional argument | `--opt[=VAL]` | Must be joined: `prefix: "--opt="`, `separate: false` |
| Negatable switch | help shows `--[no]copy`, `-[no]x` | Two booleans with the real forms, `--copy` / `--nocopy` |
| Glued short value | help shows `-Tpng`, `-q20` style | `separate: false` |
| Array prefix (auto, warn) | `--variant a b c` | If the tool wants `--variant a --variant b`, put `prefix` on the items' `inputBinding` |
| Usage-only help loses flags (auto, warn) | help `seqtk comp [-u] [-r in.bed] <in.fa>`; CWL has `bed_file` as position 2, no `-u` | Bind `-r` on `bed_file`, add `-u` as a boolean. `check_cwl.py` lists help flags no input binds (`help_flags_not_wrapped`). |
| Duplicate input id (auto) | two `mode` inputs for `-glob` and `-loc` | Distinct options need distinct ids; drop exact copies |
| Meta options as inputs | `--help`, `--version` | Remove them |

## 3. Input types

| Pattern | Example | Fix |
|---|---|---|
| Output path typed File (auto) | `out_file: File` for `-o` | A File must exist before the run. Make it `string` and add an output whose glob is `$(inputs.out_file)`. |
| Output or temp directory typed Directory (auto) | `--outdir: Directory`, `--tmp-dir: Directory` | Directory inputs are staged read-only. Make it `string`; collect it with a `Directory` output. |
| Input path typed string | `--reference: string` | `File` (or `Directory`), so it is staged |
| Index files missing | BAM without `.bai`, FASTA without `.fai`, `bwa mem` index | `secondaryFiles` on the input: `^.bai`/`.bai`, `.fai`, `.amb .ann .bwt .pac .sa` |
| YAML reads a pattern as a number | `secondaryFiles: .0123` | Quote it: `'.0123'`, `'-1'` |
| Required in usage but optional in CWL | `usage: tool -i FILE` with `File?` | Make it required |

## 4. Outputs

| Pattern | Example | Fix |
|---|---|---|
| Placeholder glob (auto) | `glob: "*.out"` with no `stdout: x.out` | Glob the files the tool really writes, from its output-path input |
| Glob refers to a missing input (auto) | `$(inputs.prefix)*` with no `prefix` input | Add the input, or fix the name |
| Prefix outputs not collected | `-o PREFIX` writes `PREFIX.bam`, `PREFIX.log` | One output with `glob: $(inputs.prefix)*`, or one per known suffix |
| Output index mismatch | glob `x.vcf.gz` with `secondaryFiles: .idx` | GATK writes `.tbi` for `.vcf.gz`, `.idx` for `.vcf`: match the name |
| Output written outside the output dir | tool writes beside its input | `InitialWorkDirRequirement` with `writable: true`, or pass an output path |
| Small float rendered as `0` | cwltool 3.1.20230425 – 3.2.x prints a `float`/`double` value below `1e-6` (`1e-8`, `1.5e-7`) as `0`, from a job file or a `default` (cwltool issue #2104) | Not a CWL error: use cwltool ≥ 3.3.20260925135507, which prints `0.00000001`. Do not rewrite the CWL; a `valueFrom: $(String(self))` workaround gives `1e-8`, which some tools cannot parse |
| Directory output has a link into the image | `Input object failed validation: No such file ... /usr/local/...` after the tool succeeded (abeona leaves `out/assemble.nf` linked to the image copy) | `ShellCommandRequirement` and an argument `&& rm -f $(inputs.out_dir)/<link>` at a high position (runs only on success) |

## 5. Help-text problems that poison generation

Generating from bad help invents flags. Recapture with `container_help.py help` first.

| Recorded "help" | Cause |
|---|---|
| `FATAL: ... building SIF`, `no space left on device`, `Unable to find image` | Container engine failed; nothing ran |
| Apptainer `INFO:` lines or Docker pull progress before the help | Engine log mixed in; strip it (the scripts do) |
| Python traceback | Host `~/.local` packages leaked into the container (run with `--cleanenv` and an empty home), or the tool needs an argument |
| `unrecognized option '--help'` and nothing else | Tool uses `-h`/`-help`, or prints usage only with no arguments |
| Empty output | Tool reads stdin when stdin is not a terminal: give it a terminal (the script does) |
| Paths like `/home/<user>/...` | Host home mounted; never publish these |
| An environment dump (`env`) | Wrong command; may contain secrets: never commit it |

## 6. Junk files to delete, not fix

Commands enumerated from the image that are not the package's tools: system utilities
(`art_chmod`, `blinker_tar`), interpreters (`perl-json_perl`, `blinker_python`, `pkg_prove`),
package managers (`seaborn_pip`, `pkg_cpanm`), and files named after a help word that duplicate
another file (`bindash_The`). `container_help.py list` filters these out before generation.
