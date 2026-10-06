#!/usr/bin/env python3
"""Build a container image once, list the commands it provides, and capture clean help text.

  container_help.py build IMAGE                 # pull/convert once (long timeout)
  container_help.py list  IMAGE [--all]         # candidate tool commands, one per line + kind
  container_help.py help  IMAGE CMD [SUB ...]   # best help text to stdout; verdict to stderr
  container_help.py help  IMAGE CMD --json      # all attempts with verdicts, as JSON
  container_help.py subcommands IMAGE CMD       # subcommand names parsed from CMD's help
  container_help.py clean                       # remove the scratch cache

Engine: Apptainer/Singularity when installed, else Docker (override with --engine).
All Apptainer cache and build temp files live in --cache (default ./.cwl-image-cache),
so a killed build cannot leave /tmp/build-temp-* folders behind; `clean` removes them.
Containers run with a clean environment and an empty home (/home/user): the host's
~/.local Python packages must not shadow the image's, and help must not print host paths.
"""
import argparse
import json
import os
import time
import re
import shutil
import subprocess
import sys
from pathlib import Path

INTERPRETERS = {"perl", "python", "python2", "python3", "Rscript", "R", "java", "ruby", "node",
                "julia", "lua", "php", "prove", "perldoc", "sh", "bash", "dash", "zsh", "tclsh",
                "wish", "jshell", "irb", "pydoc", "pydoc3", "idle", "idle3", "ipython"}
PACKAGE_MANAGERS = {"pip", "pip3", "conda", "mamba", "micromamba", "cpan", "cpanm", "gem", "npm",
                    "npx", "apt", "apt-get", "yum", "dnf", "easy_install", "wheel", "bundle"}
SYSTEM_UTILITIES = set("""
    ls cat less more head tail grep egrep fgrep sed awk gawk mawk sort uniq cut tr wc find xargs du
    df chmod chown chgrp ln rm cp mv mkdir rmdir touch man top ps kill ip ifconfig ping
    traceroute netstat ss which whereis env echo printf tar gzip gunzip zcat bzip2 bunzip2 xz
    unxz unzip zip curl wget date sleep tee file stat readlink realpath basename dirname id
    whoami uname hostname mount umount dd diff cmp patch yes true false test expr seq nproc
    free uptime vi vim nano su sudo chroot nohup time timeout watch lsof tput clear reset stty
    ldd strings od hexdump base64 md5sum sha1sum sha256sum split join paste comm fold fmt nl
    pr rev shuf tac openssl pigz lz4 zstd sqlite3 xmllint iconv locale getopt infocmp tic toe
""".split())
# build tooling and library helpers that conda environments put on PATH
NOISE_RE = re.compile(
    r"(-config$|^x86_64-|^aarch64-|^2to3|^f2py|^c_rehash$|^ncurses|^tabs$|^captoinfo$|^infotocap$|"
    r"^xz(cat|cmp|diff|egrep|fgrep|grep|less|more|dec)$|^lz(cat|cmp|diff|egrep|fgrep|grep|less|ma|more)$|"
    r"^unlz|^bz(cat|cmp|diff|egrep|fgrep|grep|ip2recover|less|more)$|^z(cmp|diff|egrep|fgrep|force|grep|less|more|new)$|"
    r"^gif|^png|^libpng|^tiff|^jpeg|^cjpeg|^djpeg|^wrjpgcom|^rdjpgcom|^tclsh|^wish|^derb$|^genbrk$|"
    r"^gencfu$|^gencnval$|^gendict$|^genrb$|^icu|^makeconv$|^pkgdata$|^uconv$|^h5|^gsl-|^krb5|^k5|"
    r"^kadmin|^kdestroy$|^kinit$|^klist$|^kpasswd$|^ksu$|^kswitch$|^ktutil$|^kvno$|^sclient$|"
    r"^sim_|^uuclient$|^compile_et$|^gss-|^curl-|^idn2$|^xslt|^xml2|^lzmainfo$|^unpigz$|^bsd|"
    r"^gettext|^msg|^envsubst$|^ngettext$|^autopoint$|^recode-sr-latin$|^openssl$|^perl5|^pod|"
    r"^cpan|^corelist$|^enc2xs$|^encguess$|^h2ph$|^h2xs$|^instmodsh$|^json_pp$|^libnetcfg$|"
    r"^piconv$|^pl2pm$|^prove$|^ptar|^shasum$|^splain$|^streamzip$|^xsubpp$|^zipdetails$|"
    r"^python\d|^pydoc|^idle|^pip\d|^R$|^Rscript$|^java$|^jar$|^keytool$|^jrunscript$|^rmid$|"
    r"^rmiregistry$|^unpack200$|^pack200$|^tqdm$|^normalizer$|^chardetect$|^markdown|^pygmentize$|"
    r"^isympy$|^jsonschema$|^wheel$|^easy_install|^cython|^cygdb$|^tabulate$|^jupyter|^ipython|"
    r"^adig$|^ahost$|^keyctl$|^libdeflate-|^lzmadec$|^sqlite3_analyzer$|^tset$|^reset$|^clear$|^perlbug$|^perlivp$|^perlthanks$)"
)
ENGINE_LOG_RE = re.compile(
    r"(?m)^(?:(?:INFO|WARNING|VERBOSE|DEBUG):\s{2,}\S.*"
    r"|Unable to find image '[^']+' locally|\S+: Pulling from \S+"
    r"|[0-9a-f]{12}: (?:Pulling fs layer|Waiting|Downloading.*|Verifying Checksum|Download complete"
    r"|Extracting.*|Pull complete|Already exists)"
    r"|Digest: sha256:[0-9a-f]+|Status: (?:Downloaded newer image|Image is up to date) for \S+"
    r")[ \t]*\r?(?:\n|\Z)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ENGINE_ERROR_RE = re.compile(r"no space left on device|pull access denied|error response from daemon|"
                             r"unable to handle docker://|FATAL:\s|toomanyrequests|auth token", re.I)
NOT_FOUND_RE = re.compile(r"executable file not found|: not found$|command not found|"
                          r"exec: \"?\S+\"?: no such file", re.I | re.M)
# the tool took the help flag as an input file (`Can't open --help`, `fail to open file '--help'`)
FLAG_AS_FILE_RE = re.compile(r"(open|read|find|access)\w*\s+(file\s+)?['\"`]?-{1,2}h(elp)?\b", re.I)
USAGE_RE = re.compile(r"(?im)^\s*usage:?\s+\S")
CRASH_RE = re.compile(r"Traceback \(most recent call last\)|Exception in thread|panicked at|"
                      r"Segmentation fault|core dumped", re.I)
REJECTED_RE = re.compile(r"unrecognized option '--?h(elp)?'|invalid option -- '?h'?|unknown (flag|option)"
                         r"[:]? -?-?h(elp)?|ignored explicit argument 'elp'|unrecognized arguments: -?-?h",
                         re.I)
OPTION_LINE_RE = re.compile(r"(?m)^[ \t]*(?:-{1,2}[A-Za-z0-9]|\[-{1,2}[A-Za-z0-9])")
FLAG_RE = re.compile(r"(?m)(?:^|[\s\[(|,])-{1,2}[A-Za-z][\w-]*|(?:^|[\s\[])[A-Za-z]\w*=[<\[\w]")
HELP_OPTS = ["--help", "-h", "-help", None]
# words that help sentences put where a subcommand list would be (bindash_The, skani_options)
STRAY_WORDS = set("""the a an and or of to for in on from with by this that these those see print path
number enable log run generate download additional specified all files output results command
subcommand commands usage options option help valid error warning note example default if when
is are be it its use using version""".split())
COMMANDS_HEADER_RE = re.compile(r"(?i)^\s*(?:available\s+|positional\s+)?(?:sub-?)?(?:commands?|tools|programs)"
                                r"\s*(?:are|include)?\s*:?\s*(.*)$")
COMMAND_ROW_RE = re.compile(r"^\s*([A-Za-z][\w.+-]*)(?:\s*,\s*[A-Za-z][\w.+-]*)?(?:\s{2,}|\t+)\S")
CHOICES_RE = re.compile(r"\{([A-Za-z][\w.-]*(?:,[A-Za-z][\w.-]*)+)\}")


def parse_subcommands(text):
    """Subcommand names from a help's command list (`Command: seq  common ...`) or an
    argparse choice set (`{index,query}`); stray help words and flags are dropped."""
    names = []
    for m in CHOICES_RE.finditer(text):
        names += m.group(1).split(",")
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = COMMANDS_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        rows = [m.group(1)] if m.group(1) else []
        blank = 0
        for line in lines[i + 1:]:
            if not line.strip():
                blank += 1
                if blank > 1:
                    break
                continue
            blank = 0
            if not line[:1].isspace() and line.rstrip().endswith(":"):
                break  # next section header (Options:)
            rows.append(line)
        for row in rows:
            r = COMMAND_ROW_RE.match(row if row[:1].isspace() else "  " + row)
            if r:
                names.append(r.group(1))
        i += 1
    seen = []
    for n in names:
        if n.lower() not in STRAY_WORDS and n not in seen and not n.startswith("-"):
            seen.append(n)
    return seen


def engine_of(choice):
    if choice != "auto":
        return choice
    for e in ("apptainer", "singularity"):
        if shutil.which(e):
            return e
    if shutil.which("docker"):
        return "docker"
    sys.exit("no container engine found (apptainer, singularity or docker)")


def env_for(cache):
    tmp = Path(cache) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for k in ("SINGULARITY_CACHEDIR", "APPTAINER_CACHEDIR"):
        env[k] = str(Path(cache).resolve())
    for k in ("SINGULARITY_TMPDIR", "APPTAINER_TMPDIR", "TMPDIR"):
        env[k] = str(tmp.resolve())
    return env


def uri(image):
    return image if "://" in image else f"docker://{image}"


def container_cmd(engine, image, argv, cache):
    if engine == "docker":
        return ["docker", "run", "--rm", "-w", "/tmp", "-e", "PYTHONNOUSERSITE=1", image] + argv
    home = Path(cache).resolve() / "home"
    home.mkdir(parents=True, exist_ok=True)
    return [engine, "exec", "--cleanenv", "--home", f"{home}:/home/user", "--pwd", "/tmp",
            "--env", "PYTHONNOUSERSITE=1", uri(image)] + argv


def run(engine, image, argv, cache, timeout):
    # stdin is a terminal: many tools (seqtk, samtools subcommands) print usage only when
    # isatty(stdin); with a pipe they wait for data on stdin and print nothing
    master, slave = os.openpty()
    argv = container_cmd(engine, image, argv, cache)
    name = None
    if engine == "docker":
        # a named container: killing the client on timeout leaves the container running
        # (a server command such as `auspice view` never exits), so remove it by name
        name = f"cwl-help-{os.getpid()}-{time.monotonic_ns()}"
        argv[2:2] = ["-i", "--name", name]
    try:
        p = subprocess.run(argv, stdin=slave, capture_output=True, timeout=timeout,
                           env=env_for(cache), cwd="/tmp")
    except subprocess.TimeoutExpired:
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return None, "timeout"
    finally:
        os.close(slave)
        os.close(master)
    out = (p.stdout + p.stderr).decode("utf-8", errors="replace")
    # colour codes first: a coloured "INFO:" line would not match the engine-log pattern
    return p.returncode, ENGINE_LOG_RE.sub("", ANSI_RE.sub("", out)).strip()


def build(engine, image, cache, timeout=1800):
    if engine == "docker":
        p = subprocess.run(["docker", "pull", image], capture_output=True, text=True, timeout=timeout)
    else:
        # converting once with a long timeout; a short help timeout would kill the build mid-way
        p = subprocess.run([engine, "exec", "--cleanenv", uri(image), "true"], capture_output=True,
                           text=True, timeout=timeout, env=env_for(cache), cwd="/tmp")
        if p.returncode != 0 and "Using cached SIF image" not in p.stderr + p.stdout:
            # `true` may be absent from a minimal image; the SIF is still cached if it built
            if ENGINE_ERROR_RE.search(p.stderr + p.stdout):
                return False, (p.stderr or p.stdout).strip().splitlines()[-1:]
    if engine == "docker" and p.returncode != 0:
        return False, (p.stderr or p.stdout).strip().splitlines()[-1:]
    return True, []


def classify(code, text):
    if code is None:
        return "timeout"
    if NOT_FOUND_RE.search(text) and len(text) < 400:
        return "not_found"  # before engine errors: Apptainer says `FATAL: "x": executable file not found`
    if ENGINE_ERROR_RE.search(text):
        return "engine_error"
    rejected = REJECTED_RE.search(text)
    options = len(OPTION_LINE_RE.findall(text))
    if FLAG_AS_FILE_RE.search(text) and not USAGE_RE.search(text):
        return "flag_as_file"
    if rejected and options < 3:
        return "flag_rejected"
    if CRASH_RE.search(text) and options < 3:
        return "crash"
    if len(parse_subcommands(text)) >= 2 and options < 3:
        return "subcommands"
    if options < 3 and USAGE_RE.search(text) and not CRASH_RE.search(text):
        # a usage line is the whole help of many small tools (`Usage: bwa fa2pac [-f] <in.fasta>`)
        return "usage_only"
    if not FLAG_RE.search(text):
        return "no_options"
    if len(text) < 60:
        return "too_short"
    return "ok"


def best_help(engine, image, cmd, cache, timeout):
    """Try each help form; keep the ok one with the most option lines. Never drop the program
    name: retrying `samtools sort` as `sort` captures GNU sort's help instead."""
    attempts = []
    for opt in HELP_OPTS:
        argv = cmd + ([opt] if opt else [])
        code, text = run(engine, image, argv, cache, timeout if opt else min(timeout, 15))
        verdict = classify(code, text)
        attempts.append({"argv": argv, "exit": code, "verdict": verdict,
                         "option_lines": len(OPTION_LINE_RE.findall(text)), "text": text})
        if verdict == "not_found":
            break
    ok = [a for a in attempts if a["verdict"] == "ok"] or \
        [a for a in attempts if a["verdict"] == "usage_only"]
    # most option lines; on a tie prefer the form the tool accepted (no "invalid option" noise)
    best = max(ok, key=lambda a: (a["option_lines"], not REJECTED_RE.search(a["text"]),
                                  -len(a["text"]))) if ok else None
    return best, attempts


def list_commands(engine, image, cache, timeout, show_all):
    script = ("for d in /usr/local/bin /opt/conda/bin /opt/*/bin; do [ -d \"$d\" ] && "
              "find \"$d\" -maxdepth 1 \\( -type f -o -type l \\) -perm -u+x 2>/dev/null; done")
    code, text = run(engine, image, ["sh", "-c", script], cache, timeout)
    if code is None or code != 0 and not text:
        sys.exit(f"cannot list commands: {text}")
    seen = {}
    for path in text.splitlines():
        name = path.rsplit("/", 1)[-1].strip()
        if not name or name in seen:
            continue
        if name in INTERPRETERS:
            kind = "interpreter"
        elif name in PACKAGE_MANAGERS:
            kind = "package_manager"
        elif name in SYSTEM_UTILITIES:
            kind = "system_utility"
        elif NOISE_RE.search(name):
            kind = "library_helper"
        else:
            kind = "tool"
        seen[name] = (kind, path.strip())
    for name, (kind, path) in sorted(seen.items()):
        if show_all or kind == "tool":
            print(f"{name}\t{kind}\t{path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["build", "list", "help", "subcommands", "clean"])
    ap.add_argument("image", nargs="?")
    ap.add_argument("cmd", nargs="*", help="command words for `help`, e.g. samtools sort")
    ap.add_argument("--engine", default="auto", choices=["auto", "apptainer", "singularity", "docker"])
    ap.add_argument("--cache", default=".cwl-image-cache")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per help call")
    ap.add_argument("--all", action="store_true", help="list: also show interpreters, utilities")
    ap.add_argument("--json", action="store_true", help="help: print every attempt as JSON")
    a = ap.parse_args()
    if a.action == "clean":
        shutil.rmtree(a.cache, ignore_errors=True)
        print(f"removed {a.cache}")
        return
    if not a.image:
        ap.error("IMAGE is required")
    engine = engine_of(a.engine)
    if a.action == "build":
        ok, detail = build(engine, a.image, a.cache)
        print(("built " if ok else "FAILED ") + a.image + ("" if ok else f": {detail}"))
        sys.exit(0 if ok else 1)
    if a.action == "list":
        list_commands(engine, a.image, a.cache, max(a.timeout, 120), a.all)
        return
    if not a.cmd:
        ap.error("help needs a command, e.g. help IMAGE samtools sort")
    if a.action == "subcommands":
        _, attempts = best_help(engine, a.image, a.cmd, a.cache, a.timeout)
        names = []
        for x in attempts:
            names += [n for n in parse_subcommands(x["text"]) if n not in names]
        print("\n".join(names))
        print(f"[subcommands] {' '.join(a.cmd)}: {len(names)} candidates; confirm each with "
              f"`help IMAGE {' '.join(a.cmd)} <name>`", file=sys.stderr)
        sys.exit(0 if names else 2)
    best, attempts = best_help(engine, a.image, a.cmd, a.cache, a.timeout)
    if a.json:
        print(json.dumps({"best": best and best["argv"], "attempts": attempts}, indent=1))
    elif best:
        print(best["text"])
    summary = ", ".join(f"{' '.join(x['argv'][len(a.cmd):]) or '(no args)'}={x['verdict']}" for x in attempts)
    if not best and any(x["verdict"] == "subcommands" for x in attempts):
        print(next(x["text"] for x in attempts if x["verdict"] == "subcommands"))
        print(f"[help] {' '.join(a.cmd)}: lists subcommands; run `subcommands` and wrap each one",
              file=sys.stderr)
        sys.exit(3)
    print(f"[help] {' '.join(a.cmd)}: {'ok via ' + ' '.join(best['argv']) if best else 'NO USABLE HELP'}"
          f" ({summary})", file=sys.stderr)
    sys.exit(0 if best else 2)


if __name__ == "__main__":
    main()
