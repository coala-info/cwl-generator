#!/usr/bin/env python3
"""Write a small, consistent set of real bioinformatics test files.

  make_testdata.py OUTDIR                    # text formats + BAM/BAI + VCF.GZ/TBI (needs a container engine)
  make_testdata.py OUTDIR --no-container     # text formats only

Everything derives from one random reference (seeded, so reruns give the same files):

  ref.fa  ref.fa.fai  ref.dict          2 contigs (chr1 2000 bp, chr2 1000 bp)
  reads_1.fq  reads_2.fq  reads.fq      60 read pairs (100 bp) sampled from chr1, Phred+33
  reads.sam                             the same pairs, aligned (exact positions), coordinate-sorted
  reads.bam  reads.bam.bai  reads.bai   via samtools in a container
  variants.vcf                          5 SNPs on chr1 with the true REF bases, one sample
  variants.vcf.gz  variants.vcf.gz.tbi  via bgzip/tabix in a container
  regions.bed  annotation.gff3  annotation.gtf  protein.fa  table.tsv  text.txt

manifest.json lists each file with its format. Reads carry no sequencing errors, and the
variants are not in the reads: fine for "does the wrapper run", not for accuracy tests.
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

SAMTOOLS_IMAGE = "quay.io/biocontainers/samtools:1.23--h96c455f_0"
HTSLIB_IMAGE = "quay.io/biocontainers/htslib:1.23--h566b1c6_0"
COMP = str.maketrans("ACGT", "TGCA")


def wrap(seq, width=60):
    return "\n".join(seq[i:i + width] for i in range(0, len(seq), width))


def write_reference(out, rng):
    contigs = {"chr1": "".join(rng.choice("ACGT") for _ in range(2000)),
               "chr2": "".join(rng.choice("ACGT") for _ in range(1000))}
    fa, fai, offset = [], [], 0
    for name, seq in contigs.items():
        header = f">{name}\n"
        body = wrap(seq) + "\n"
        offset += len(header)
        fai.append(f"{name}\t{len(seq)}\t{offset}\t60\t61")
        offset += len(body)
        fa.append(header + body)
    (out / "ref.fa").write_text("".join(fa))
    (out / "ref.fa.fai").write_text("\n".join(fai) + "\n")
    dict_lines = ["@HD\tVN:1.6"] + [
        f"@SQ\tSN:{n}\tLN:{len(s)}\tM5:{hashlib.md5(s.encode()).hexdigest()}\tUR:file:ref.fa"
        for n, s in contigs.items()]
    (out / "ref.dict").write_text("\n".join(dict_lines) + "\n")
    return contigs


def write_reads(out, rng, chr1, n=60, length=100, insert=300):
    r1, r2, sam = [], [], []
    for i in range(n):
        start = rng.randrange(0, len(chr1) - insert)
        frag = chr1[start:start + insert]
        s1, s2 = frag[:length], frag[-length:].translate(COMP)[::-1]
        q = "I" * length
        name = f"read{i + 1}"
        r1.append(f"@{name}/1\n{s1}\n+\n{q}\n")
        r2.append(f"@{name}/2\n{s2}\n+\n{q}\n")
        pos1, pos2 = start + 1, start + insert - length + 1
        # 99/147: paired, proper, mate reverse / read reverse, first/second in pair
        sam.append((pos1, f"{name}\t99\tchr1\t{pos1}\t60\t{length}M\t=\t{pos2}\t{insert}\t{s1}\t{q}"))
        sam.append((pos2, f"{name}\t147\tchr1\t{pos2}\t60\t{length}M\t=\t{pos1}\t{-insert}\t"
                          f"{frag[-length:]}\t{q}"))
    (out / "reads_1.fq").write_text("".join(r1))
    (out / "reads_2.fq").write_text("".join(r2))
    shutil.copy(out / "reads_1.fq", out / "reads.fq")
    header = (out / "ref.dict").read_text().replace("\tUR:file:ref.fa", "").replace("VN:1.6", "VN:1.6\tSO:coordinate")
    header += "@RG\tID:rg1\tSM:sample1\tPL:ILLUMINA\n"
    body = "\n".join(line + "\tRG:Z:rg1" for _, line in sorted(sam, key=lambda x: x[0]))
    (out / "reads.sam").write_text(header + body + "\n")


def write_annotations(out, rng, contigs):
    chr1 = contigs["chr1"]
    vcf = ["##fileformat=VCFv4.2", '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">']
    vcf += [f"##contig=<ID={n},length={len(s)}>" for n, s in contigs.items()]
    vcf.append("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1")
    for pos in sorted(rng.sample(range(100, 1900), 5)):
        ref = chr1[pos - 1]
        alt = rng.choice([b for b in "ACGT" if b != ref])
        vcf.append(f"chr1\t{pos}\t.\t{ref}\t{alt}\t50\tPASS\t.\tGT\t0/1")
    (out / "variants.vcf").write_text("\n".join(vcf) + "\n")
    (out / "regions.bed").write_text("chr1\t100\t600\tregion1\nchr1\t900\t1500\tregion2\nchr2\t0\t500\tregion3\n")
    (out / "annotation.gff3").write_text(
        "##gff-version 3\n"
        "chr1\ttest\tgene\t201\t1100\t.\t+\t.\tID=gene1;Name=GENE1\n"
        "chr1\ttest\tmRNA\t201\t1100\t.\t+\t.\tID=tx1;Parent=gene1\n"
        "chr1\ttest\texon\t201\t500\t.\t+\t.\tID=exon1;Parent=tx1\n"
        "chr1\ttest\texon\t801\t1100\t.\t+\t.\tID=exon2;Parent=tx1\n"
        "chr1\ttest\tCDS\t201\t500\t.\t+\t0\tID=cds1;Parent=tx1\n"
        "chr1\ttest\tCDS\t801\t1100\t.\t+\t0\tID=cds1;Parent=tx1\n")
    attrs = 'gene_id "gene1"; transcript_id "tx1";'
    (out / "annotation.gtf").write_text(
        f"chr1\ttest\tgene\t201\t1100\t.\t+\t.\tgene_id \"gene1\";\n"
        f"chr1\ttest\ttranscript\t201\t1100\t.\t+\t.\t{attrs}\n"
        f"chr1\ttest\texon\t201\t500\t.\t+\t.\t{attrs} exon_number \"1\";\n"
        f"chr1\ttest\texon\t801\t1100\t.\t+\t.\t{attrs} exon_number \"2\";\n"
        f"chr1\ttest\tCDS\t201\t500\t.\t+\t0\t{attrs}\n"
        f"chr1\ttest\tCDS\t801\t1100\t.\t+\t0\t{attrs}\n")
    aa = "ACDEFGHIKLMNPQRSTVWY"
    (out / "protein.fa").write_text("".join(
        f">prot{i}\n{wrap('M' + ''.join(rng.choice(aa) for _ in range(150)))}\n" for i in (1, 2, 3)))
    (out / "table.tsv").write_text("sample\tgroup\tvalue\ns1\tA\t1.5\ns2\tA\t2.0\ns3\tB\t3.1\ns4\tB\t2.8\n")
    (out / "text.txt").write_text("hello world\nline two\nline three\n")


def engine_of():
    for e in ("apptainer", "singularity", "docker"):
        if shutil.which(e):
            return e
    return None


def in_container(engine, image, out, script, cache):
    out = str(out.resolve())
    if engine == "docker":
        cmd = ["docker", "run", "--rm", "-v", f"{out}:/data", "-w", "/data", "-u",
               f"{os.getuid()}:{os.getgid()}", image, "sh", "-c", script]
        env = None
    else:
        cache = Path(cache).resolve()
        (cache / "tmp").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, APPTAINER_CACHEDIR=str(cache), SINGULARITY_CACHEDIR=str(cache),
                   APPTAINER_TMPDIR=str(cache / "tmp"), SINGULARITY_TMPDIR=str(cache / "tmp"))
        cmd = [engine, "exec", "--cleanenv", "--no-home", "--bind", f"{out}:/data", "--pwd", "/data",
               image if "://" in image else f"docker://{image}", "sh", "-c", script]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800, cwd="/tmp")
    if p.returncode != 0:
        print(f"[container] FAILED: {script}\n{(p.stderr or p.stdout)[-600:]}", file=sys.stderr)
    return p.returncode == 0


FORMATS = {
    "ref.fa": "fasta", "ref.fa.fai": "fai", "ref.dict": "dict", "reads_1.fq": "fastq",
    "reads_2.fq": "fastq", "reads.fq": "fastq", "reads.sam": "sam", "reads.bam": "bam",
    "reads.bam.bai": "bai", "reads.bai": "bai", "variants.vcf": "vcf", "variants.vcf.gz": "vcf.gz",
    "variants.vcf.gz.tbi": "tbi", "regions.bed": "bed", "annotation.gff3": "gff3",
    "annotation.gtf": "gtf", "protein.fa": "protein_fasta", "table.tsv": "tsv", "text.txt": "text",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--no-container", action="store_true", help="skip BAM and VCF.GZ (no engine needed)")
    ap.add_argument("--samtools-image", default=SAMTOOLS_IMAGE)
    ap.add_argument("--htslib-image", default=HTSLIB_IMAGE)
    ap.add_argument("--cache", default=".cwl-image-cache")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    contigs = write_reference(out, rng)
    write_reads(out, rng, contigs["chr1"])
    write_annotations(out, rng, contigs)
    if not a.no_container:
        engine = engine_of()
        if not engine:
            print("[container] no engine found: BAM and VCF.GZ skipped", file=sys.stderr)
        else:
            in_container(engine, a.samtools_image, out,
                         "samtools sort -o reads.bam reads.sam && samtools index reads.bam && "
                         "cp reads.bam.bai reads.bai && samtools quickcheck reads.bam", a.cache)
            in_container(engine, a.htslib_image, out,
                         "bgzip -c variants.vcf > variants.vcf.gz && tabix -p vcf variants.vcf.gz", a.cache)
    manifest = [{"path": n, "format": f, "bytes": (out / n).stat().st_size}
                for n, f in FORMATS.items() if (out / n).exists()]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    missing = [n for n in FORMATS if not (out / n).exists()]
    print(f"wrote {len(manifest)} files to {out}" + (f"; missing: {missing}" if missing else ""))


if __name__ == "__main__":
    main()
