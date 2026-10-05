# Capturing help from a container

`scripts/container_help.py` encodes all of this. Read this file when the script fails, or when
running containers by hand.

## Rules

1. **Build the image once, with a long timeout.** `apptainer exec docker://IMG` converts the
   whole image to SIF on first use. Under a 30 s help timeout a large image (busco) is killed
   mid-build: every help attempt fails, and Apptainer leaves a multi-GB `/tmp/build-temp-*`
   folder behind each time. 319 such folders filled 255 GB in one session.
   Run `container_help.py build IMAGE` first (30 min limit).

2. **Keep cache and build temp in one scratch folder.** Set `APPTAINER_CACHEDIR`,
   `APPTAINER_TMPDIR` (and the `SINGULARITY_*` twins) and `TMPDIR` to a folder you delete
   afterwards (`container_help.py clean`). Never sweep `/tmp/build-temp-*` you did not create.

3. **Isolate the container from the host.**
   - `--cleanenv`: host `PYTHONPATH`, `R_LIBS`, `PERL5LIB` must not reach the tool.
   - Empty home: `--home <empty>:/home/user`. Apptainer mounts the real home by default, so
     host `~/.local/lib/python3.x/site-packages` shadows the image's packages. Tools then crash
     or print warnings, and the crash gets recorded as help. Help that prints `$HOME` paths
     (dcm2niix "Defaults file") would also put the user's home path into the report.
   - `--env PYTHONNOUSERSITE=1`, `--pwd /tmp`.
   - Docker: `docker run --rm -w /tmp -e PYTHONNOUSERSITE=1 -i IMAGE ...`.

4. **Give stdin a terminal.** Many tools (seqtk subcommands, some samtools subcommands) print
   usage only when stdin is a TTY. With a pipe they wait for data on stdin and print nothing.

5. **Try several help forms and keep the best.** `--help`, `-h`, `-help`, then no arguments.
   Keep the one with the most option lines. A tool that rejects `--help` but then prints its
   full option list (samtools: `unrecognized option '--help'` + usage) has usable help.

6. **Never drop the program name on retry.** If `samtools sort --help` fails, retrying
   `sort --help` captures GNU sort. A retry may only change the help option.

7. **Strip engine noise, not tool output.** Remove lines like `INFO:    Converting OCI blobs`,
   `Unable to find image 'x' locally`, `<layer>: Pull complete`, `Digest: sha256:`. Keep
   everything else.

8. **Classify before using.** Verdicts from `container_help.py help --json`:

| Verdict | Meaning | Action |
|---|---|---|
| ok | Usage with options | Generate |
| usage_only | Only a usage line (`Usage: bwa samse [-n max_occ] [-f out.sam] <prefix> <in.sai> <in.fq>`) | Usable: it is the whole help of many small tools; read options and positionals from it, and the source or manual for anything it does not explain |
| flag_as_file | Tool read the help flag as an input file (`Can't open --help`) | Try the other forms; scripts that read files only may have no help: read the script source and say so |
| subcommands | A command list, few options | Run `subcommands`, wrap each subcommand |
| flag_rejected | Only "invalid option" | Try the other forms; if none, read the docs |
| crash | Traceback | Check isolation (rule 3); the tool may need an argument |
| not_found | Command not in image | Fix the name with `list` |
| engine_error | Pull/build failed, disk full, registry rate limit | Fix the engine problem; retry after a wait |
| no_options | No flags at all | Tool ran instead of printing help; check docs |

9. **Registry rate limits.** Errors with `auth token`, `toomanyrequests`, `TLS handshake
   timeout` are transient. Wait (90 s, then 180 s) and retry. Keep parallel pulls to 4 or fewer.

## Discovering commands

- `container_help.py list IMAGE` lists executables in `/usr/local/bin`, `/opt/conda/bin`
  and `/opt/*/bin`, with interpreters, system utilities, package managers and library helpers
  filtered out (`--all` shows them with their kind).
- Bioconda images put everything in `/usr/local/bin`; non-conda images may use other paths
  (`/files/<tool>/` for W4M Galaxy wrappers, `/opt/<tool>/bin`). Search with
  `find / -name '<name>*' -type f 2>/dev/null` inside the container when `list` misses a tool.
- A subcommand candidate is real only if `help IMAGE PKG SUB` returns ok help that differs
  from the package's own help. Words like `The`, `Print`, `options` are sentence words.
