#!/usr/bin/env python3
r"""Find real test data for a tool in its own source repository (and other public test sets).

  find_testdata.py PACKAGE                       # list candidate files from the tool's GitHub repo
  find_testdata.py PACKAGE --repo owner/name     # skip repo lookup
  find_testdata.py PACKAGE --get 'test/*.fa' 'test/r1.fq' --out testdata
  find_testdata.py PACKAGE --galaxy              # also list Galaxy tools-iuc test-data
  find_testdata.py PACKAGE --nfcore /path/to/test-datasets --search 'sarscov2/genome/genome\.fasta$'
                                                 # nf-core/test-datasets (local clone, all branches)

Sources, in order:
  1. The bioconda recipe (bioconda-recipes/recipes/PACKAGE/meta.yaml): its `test: commands`
     are real invocations, and its source URL names the repository.
  2. The anaconda.org package record (home / dev_url) when the recipe names no GitHub repo.
  3. The repository tree: small files with bioinformatics extensions under test, example,
     data or demo folders, listed smallest first with raw download URLs.
  4. With --galaxy: galaxyproject/tools-iuc tools/PACKAGE/test-data (curated test inputs).

Nothing is downloaded unless --get is given; --get takes glob patterns matched against
the listed paths and refuses files over --max-mb. Set GITHUB_TOKEN to raise the GitHub API
rate limit (60 requests/hour without it).
"""
import argparse
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DATA_EXT = re.compile(
    r"\.(fa|fasta|fna|ffn|faa|fas|fq|fastq|sam|bam|cram|bai|crai|csi|vcf|bcf|tbi|bed|bedpe|gff|gff3|gtf|"
    r"gb|gbk|genbank|embl|sai|pac|bwt|ann|amb|sa|fai|dict|aln|maf|psl|wig|bigwig|bw|bedgraph|"
    r"nwk|newick|tre|tree|phy|sto|stk|hmm|clustal|msa|txt|tsv|csv|json|h5|loom|mtx|pdb|cif|mzml|mgf)"
    r"(\.gz|\.bgz|\.bz2|\.xz)?$", re.I)
TEST_DIR = re.compile(r"(^|/)(tests?|testdata|test[-_]data|test[-_]files|examples?|demo|data|sample[-_]?data|"
                      r"samples?|toy|fixtures?|resources?|inst/extdata|extdata)(/|$)", re.I)
SKIP = re.compile(r"(^|/)(\.github|docs?|doc|man|vendor|third[-_]party|node_modules)(/|$)|"
                  r"(^|/)(requirements|setup|pyproject|package|environment|CITATION|LICENSE|README)", re.I)


def fetch(url, accept_json=True):
    req = urllib.request.Request(url, headers={"User-Agent": "find-testdata"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 403 and "api.github.com" in url:
            sys.exit("GitHub API rate limit reached: set GITHUB_TOKEN or wait an hour")
        return None
    except urllib.error.URLError:
        return None
    return json.loads(body) if accept_json else body.decode("utf-8", errors="replace")


def github_repo(text):
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git|/archive|/releases|/tarball|/zipball|[/#?\s\"']|$)",
                  text or "")
    if m and m.group(1) not in ("bioconda", "conda-forge", "BioContainers"):
        return f"{m.group(1)}/{m.group(2)}"
    return None


def recipe(package):
    for branch in ("master", "main"):
        text = fetch(f"https://raw.githubusercontent.com/bioconda/bioconda-recipes/{branch}/recipes/{package}/meta.yaml",
                     accept_json=False)
        if text:
            return text
    return None


def recipe_tests(text):
    m = re.search(r"(?ms)^test:\s*\n(.*?)(?=^\S|\Z)", text or "")
    if not m:
        return []
    cmds = re.search(r"(?ms)^\s+commands:\s*\n(.*?)(?=^\s{0,2}\w+:|\Z)", m.group(1))
    return [re.sub(r"^\s*-\s*", "", l).strip().strip("'\"") for l in (cmds.group(1).splitlines() if cmds else [])
            if l.strip().startswith("-")]


def list_repo(repo, max_mb):
    info = fetch(f"https://api.github.com/repos/{repo}")
    if not info:
        return None, []
    branch = info.get("default_branch", "master")
    tree = fetch(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1")
    files = []
    for t in (tree or {}).get("tree", []):
        p = t.get("path", "")
        if t.get("type") != "blob" or SKIP.search(p) or not DATA_EXT.search(p):
            continue
        if not TEST_DIR.search(p) and not re.search(r"(^|/)(test|example|toy|sample)[\w.-]*\.", p, re.I):
            continue
        size = t.get("size", 0)
        if 0 < size <= max_mb * 1e6:
            files.append({"path": p, "size": size,
                          "url": f"https://raw.githubusercontent.com/{repo}/{branch}/{p}"})
    if (tree or {}).get("truncated"):
        print("[note] repository tree truncated by GitHub; listing is partial", file=sys.stderr)
    return branch, sorted(files, key=lambda f: (f["size"], f["path"]))


def list_galaxy(package):
    """tools-iuc test-data: tools/PKG/test-data, or tools/PKG/<subtool>/test-data (samtools).
    These folders often hold expected outputs too (seqtk_comp.out): compare against them."""
    base = f"https://api.github.com/repos/galaxyproject/tools-iuc/contents/tools/{package}"
    top = fetch(base) or []
    dirs = [""] if any(i.get("name") == "test-data" for i in top) else \
        [i["name"] + "/" for i in top if i.get("type") == "dir"][:10]
    out = []
    for d in dirs:
        for i in fetch(f"{base}/{d}test-data") or []:
            if i.get("type") == "file" and i.get("size", 0) > 0:
                out.append({"path": f"galaxy:{d}{i['name']}", "size": i["size"], "url": i.get("download_url")})
    return sorted(out, key=lambda f: (f["size"], f["path"]))


def nfcore_index(clone):
    """(branch, size, path) for every file on every branch of a local nf-core/test-datasets
    clone; cached in <clone>/.git/cwl-testdata-index.tsv (rebuilt when refs change)."""
    import subprocess
    git = ["git", "-C", str(clone)]
    cache = Path(clone) / ".git" / "cwl-testdata-index.tsv"
    refs = subprocess.run(git + ["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/remotes"],
                          capture_output=True, text=True).stdout
    stamp = str(abs(hash(refs)))
    if cache.exists() and cache.read_text().split("\n", 1)[0] == stamp:
        rows = cache.read_text().split("\n")[1:]
    else:
        rows = []
        for line in refs.splitlines():
            ref = line.split()[0]
            if ref.endswith("/HEAD"):
                continue
            out = subprocess.run(git + ["ls-tree", "-r", "-l", ref], capture_output=True, text=True).stdout
            for t in out.splitlines():
                meta, path = t.split("\t", 1)
                parts = meta.split()
                if parts[1] == "blob":
                    rows.append(f"{ref}\t{parts[3]}\t{path}")
        cache.write_text(stamp + "\n" + "\n".join(rows))
    return [r.split("\t") for r in rows if r]


def list_nfcore(clone, pattern, max_mb):
    """Matches of a regex over 'branch:path' in nf-core/test-datasets: a local clone (all
    branches) or, without one, the GitHub tree of the shared `modules` branch."""
    rx = re.compile(pattern or ".", re.I)
    out = []
    if clone:
        for ref, size, path in nfcore_index(clone):
            branch = ref.split("/", 1)[-1]
            key = f"{branch}:{path}"
            if rx.search(key) and 0 < int(size) <= max_mb * 1e6:
                out.append({"path": f"nfcore:{key}", "size": int(size), "ref": ref, "file": path,
                            "clone": str(clone)})
    else:
        tree = fetch("https://api.github.com/repos/nf-core/test-datasets/git/trees/modules?recursive=1") or {}
        for t in tree.get("tree", []):
            key = f"modules:{t.get('path')}"
            if t.get("type") == "blob" and rx.search(key) and 0 < t.get("size", 0) <= max_mb * 1e6:
                out.append({"path": f"nfcore:{key}", "size": t["size"],
                            "url": f"https://raw.githubusercontent.com/nf-core/test-datasets/modules/{t['path']}"})
    return sorted(out, key=lambda f: (f["size"], f["path"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package")
    ap.add_argument("--repo", help="owner/name on GitHub (default: from the bioconda recipe)")
    ap.add_argument("--galaxy", action="store_true", help="also list Galaxy tools-iuc test-data")
    ap.add_argument("--max-mb", type=float, default=5, help="ignore files larger than this (MB)")
    ap.add_argument("--limit", type=int, default=60, help="files to list")
    ap.add_argument("--get", nargs="+", metavar="GLOB", help="download listed files matching these globs")
    ap.add_argument("--out", default="testdata", help="download folder")
    ap.add_argument("--nfcore", nargs="?", const="", metavar="CLONE",
                    help="also search nf-core/test-datasets: a local clone (all branches), or with no "
                         "value the GitHub 'modules' branch")
    ap.add_argument("--search", metavar="REGEX", help="with --nfcore: regex over 'branch:path' "
                                                      "(e.g. 'modules:.*sarscov2/genome/genome\\.fasta$')")
    a = ap.parse_args()

    rec = recipe(a.package)
    repo = a.repo or github_repo(rec)
    if not repo:
        pkg = fetch(f"https://api.anaconda.org/package/bioconda/{a.package}") or {}
        repo = github_repo(" ".join(str(pkg.get(k, "")) for k in ("dev_url", "home", "doc_url", "html_url")))
    print(f"[recipe] {'found' if rec else 'not found'}: bioconda-recipes/recipes/{a.package}/meta.yaml")
    for c in recipe_tests(rec):
        print(f"    test command: {c}")
    files = []
    if repo:
        branch, files = list_repo(repo, a.max_mb)
        print(f"[repo] github.com/{repo} ({branch or 'unreachable'}): {len(files)} candidate data files")
    else:
        print("[repo] no GitHub repository found; pass --repo owner/name")
    if a.galaxy:
        g = list_galaxy(a.package)
        print(f"[galaxy] tools-iuc/tools/{a.package}/test-data: {len(g)} files")
        files += g
    if a.nfcore is not None:
        clone = Path(a.nfcore) if a.nfcore else None
        n = list_nfcore(clone, a.search, a.max_mb)
        print(f"[nf-core] {'local clone ' + str(clone) if clone else 'GitHub modules branch'}"
              f"{' matching ' + repr(a.search) if a.search else ''}: {len(n)} files")
        files += n
    for f in files[:a.limit]:
        print(f"  {f['size']:>9,d}  {f['path']}")
    if len(files) > a.limit:
        print(f"  ... {len(files) - a.limit} more (raise --limit)")
    if not a.get:
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    def names(f):
        bare = f["path"].split(":", 1)[-1]          # drop the "galaxy:" source tag
        return (f["path"], bare, Path(bare).name)
    chosen = [f for f in files if any(fnmatch.fnmatch(n, g) for n in names(f) for g in a.get)]
    if not chosen:
        sys.exit("no listed file matches --get")
    for f in chosen:
        dest = out / Path(f["path"].split(":", 1)[-1]).name
        if f.get("clone"):                   # local clone: read the blob, no checkout
            import subprocess
            blob = subprocess.run(["git", "-C", f["clone"], "show", f"{f['ref']}:{f['file']}"],
                                  capture_output=True, check=True).stdout
            dest.write_bytes(blob)
            f["url"] = f"https://github.com/nf-core/test-datasets/blob/{f['ref'].split('/', 1)[-1]}/{f['file']}"
        else:
            req = urllib.request.Request(f["url"], headers={"User-Agent": "find-testdata"})
            with urllib.request.urlopen(req, timeout=120) as r:
                dest.write_bytes(r.read())
        print(f"[get] {f['path']} -> {dest} ({dest.stat().st_size:,d} B)")
    manifest = out / "sources.json"
    old = json.loads(manifest.read_text()) if manifest.exists() else []
    old += [{"file": Path(f["path"].split(":", 1)[-1]).name, "url": f["url"]} for f in chosen]
    manifest.write_text(json.dumps(old, indent=1) + "\n")
    print(f"[get] provenance in {manifest}")


if __name__ == "__main__":
    main()
