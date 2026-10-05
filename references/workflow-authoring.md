# Writing a CWL Workflow (v1.2)

Spec: https://www.commonwl.org/v1.2/Workflow.html

## Contents
1. Design before YAML
2. Template and wiring rules
3. Data-flow patterns (scatter, per-sample names, subworkflow, conditional, merge)
4. Requirements
5. Index files across steps
6. Worked example (tested)
7. Errors that validate but break

## 1. Design before YAML

1. Write the pipeline as a list: step → tool command → inputs it consumes → outputs it makes.
2. Every step's tool must already pass the tool gates (help → CWL → checks → real run).
   Write the missing ones first with the tool workflow in SKILL.md.
3. Decide what varies per sample (scatter) and what is shared (reference, index, databases).
4. Decide the workflow outputs: only final results, not every intermediate.

## 2. Template and wiring rules

```yaml
cwlVersion: v1.2
class: Workflow
label: my_pipeline
doc: "One sentence: what goes in, what comes out."
requirements:
  ScatterFeatureRequirement: {}        # only the features used (section 4)
inputs:
  genome: {type: File, doc: "Reference FASTA."}
steps:
  index:
    run: tools/bwa_index.cwl           # path relative to this file
    in:
      sequences: genome                # <tool input id>: <source>
    out: [indexed_reference]           # ids of the tool's outputs this workflow uses
outputs:
  index:
    type: File
    outputSource: index/indexed_reference
```

- A step's `in` keys are the tool's input ids. A key the tool does not have is silently
  ignored by runners unless an expression uses it (`valueFrom`, `when`): check spelling.
- Every required tool input must be connected, defaulted in the tool, or given
  `default:` in the step. Optional tool inputs can be left out.
- `source` is a workflow input id or `step_id/output_id`. `out` lists only ids the tool's
  `outputs` declares.
- Types must match: `File` to `File`, `File[]` to `File[]`. A scattered step turns each
  output into an array (one level per scattered input with `nested_crossproduct`, one level
  otherwise). Feeding that array into a later step needs a scatter there too, or a tool input
  that takes an array.
- Step input `default` is used when the source is missing or null. `valueFrom` runs after
  source and default, with `self` = that value and `inputs` = all of this step's inputs.
- Put `DockerRequirement` in each tool (each tool runs in its own image). Workflow-level
  requirements are inherited by steps; step requirements override them.
- Keep tools in `tools/` and the workflow beside it; reference subworkflows by path.
- Inside `{...}` flow mappings quote types with `?` or `[]` (`{type: "File?"}`): cwltool's
  parser accepts them bare, other YAML parsers (PyYAML, many linters) do not.

## 3. Data-flow patterns

**Scatter over samples (parallel lists, same order):**
```yaml
  align:
    run: align_sample.cwl
    scatter: [reads1, reads2, sample]
    scatterMethod: dotproduct          # i-th of each list together
    in:
      reference: index/indexed_reference   # shared: not scattered
      reads1: reads1                   # File[] workflow input
      reads2: reads2
      sample: samples                  # string[]
    out: [bam]
```
`flat_crossproduct` runs every combination (parameter sweeps); `nested_crossproduct` does
the same but keeps one array level per scattered input.

**Name outputs per shard.** A tool whose output name is a default (`aligned.sam`) writes the
same name in every shard. Set it from the sample:
```yaml
      output_name:
        source: sample
        valueFrom: $(self).sam
      read_group:
        valueFrom: "@RG\\tID:$(inputs.sample)\\tSM:$(inputs.sample)"   # inputs.* = this step's inputs
```
Parameter references like `$(self)` and `$(inputs.sample)` need only
`StepInputExpressionRequirement`, not `InlineJavascriptRequirement`.

**Subworkflow per sample** (`run:` points at a Workflow; needs
`SubworkflowFeatureRequirement` in the parent). Scatter the subworkflow, not each step: one
scatter, and per-sample logic stays readable.

**Conditional step:**
```yaml
  expand:
    run: tools/bwa_xa2multi.pl.cwl
    when: $(inputs.expand_xa)          # the step must have an `expand_xa` input
    in:
      sam: mem/alignments
      expand_xa: expand_xa
    out: [expanded]
```
A skipped step's outputs are `null`: type every sink optional (`File?`, or array items
`["null", File]`). `pickValue` (`first_non_null`, `the_only_non_null`, `all_non_null`)
applies only to several sources, e.g. two alternative steps feeding one output:
```yaml
  final:
    type: File
    outputSource: [aligner_a/bam, aligner_b/bam]
    pickValue: the_only_non_null       # needs MultipleInputFeatureRequirement
```

**Merge several sources into one array input:**
```yaml
      inputs:
        source: [step_a/out, step_b/out]
        linkMerge: merge_flattened     # merge_nested (default) makes [[a], [b]]
```

**Values computed between steps** (a file name, a list): prefer `valueFrom` on the step
input; use an `ExpressionTool` only when several steps need the computed value.

## 4. Requirements

| Feature used | Requirement (workflow level) |
|---|---|
| `scatter` | `ScatterFeatureRequirement` |
| `run:` is a Workflow | `SubworkflowFeatureRequirement` |
| several `source`s or `outputSource`s | `MultipleInputFeatureRequirement` |
| `valueFrom` on a step input | `StepInputExpressionRequirement` |
| JavaScript (`${...}`, method calls like `.replace()`) | `InlineJavascriptRequirement` |
| `when` with a parameter reference only | none |

## 5. Index files across steps

- Declare `secondaryFiles` on every **workflow input** whose File needs index files. A job
  file gives only the primary path; the runner finds `ref.fa.bwt` etc. only when the
  workflow input asks for them. Without it, the step fails with missing secondary files.
- At a subworkflow boundary, copy the tool input's `secondaryFiles` exactly, including
  optional patterns (`{pattern: .alt, required: false}`). cwltool reports a mismatch only as
  "Source ... may be incompatible with sink ...".
- A step output carries its `secondaryFiles` to the next step when the tool declares them
  on the output (an index step returning the FASTA plus `.amb .ann .bwt .pac .sa`).
- Output names decide index types: GATK writes `.tbi` for `x.vcf.gz`, `.idx` for `x.vcf`;
  match the output's `secondaryFiles` to the name the workflow passes.

## 6. Worked example (tested)

`bwa_align.cwl`: index once, then per sample align, sort and index; optional XA expansion.
Run on two samples: each BAM sorted, `@RG ID:sampleA`/`sampleB`, `.csi` index; with
`expand_xa: false` the conditional outputs are `null`.

```yaml
# bwa_align.cwl
cwlVersion: v1.2
class: Workflow
label: bwa_align
doc: "Index a genome once with bwa, then align, sort and index each sample's paired reads."
requirements:
  SubworkflowFeatureRequirement: {}
  ScatterFeatureRequirement: {}
inputs:
  genome: {type: File, doc: "Reference FASTA."}
  reads1: {type: "File[]", doc: "First-in-pair FASTQ, one per sample."}
  reads2: {type: "File[]", doc: "Second-in-pair FASTQ, same order."}
  samples: {type: "string[]", doc: "Sample names, same order."}
  expand_xa: {type: boolean, default: false}
steps:
  index:
    run: tools/bwa_index.cwl
    in: {sequences: genome}
    out: [indexed_reference]
  align:
    run: align_sample.cwl
    scatter: [reads1, reads2, sample]
    scatterMethod: dotproduct
    in:
      reference: index/indexed_reference
      reads1: reads1
      reads2: reads2
      sample: samples
      expand_xa: expand_xa
    out: [bam, expanded_sam]
outputs:
  bams: {type: "File[]", outputSource: align/bam}
  expanded_sams:
    type: {type: array, items: ["null", File]}
    outputSource: align/expanded_sam
```

```yaml
# align_sample.cwl
cwlVersion: v1.2
class: Workflow
label: align_sample
requirements:
  StepInputExpressionRequirement: {}
inputs:
  reference:
    type: File
    secondaryFiles: [.amb, .ann, .bwt, .pac, .sa, {pattern: .alt, required: false}]
  reads1: File
  reads2: File
  sample: string
  expand_xa: {type: boolean, default: false}
steps:
  mem:
    run: tools/bwa_mem.cwl
    in:
      reference: reference
      reads: reads1
      mates: reads2
      sample: sample
      read_group: {valueFrom: "@RG\\tID:$(inputs.sample)\\tSM:$(inputs.sample)"}
      output_name: {source: sample, valueFrom: $(self).sam}
    out: [alignments]
  sort:
    run: tools/samtools_sort.cwl
    in:
      alignments: mem/alignments
      output_name: {source: sample, valueFrom: $(self).bam}
      write_index: {default: true}
    out: [sorted]
  expand:
    run: tools/bwa_xa2multi.pl.cwl
    when: $(inputs.expand_xa)
    in: {sam: mem/alignments, expand_xa: expand_xa}
    out: [expanded]
outputs:
  bam: {type: File, outputSource: sort/sorted}
  expanded_sam: {type: "File?", outputSource: expand/expanded}
```

Job (arrays as YAML lists, one entry per sample, same order):
```yaml
genome: {class: File, path: testdata/ref.fa}
reads1: [{class: File, path: testdata/sA_1.fq}, {class: File, path: testdata/sB_1.fq}]
reads2: [{class: File, path: testdata/sA_2.fq}, {class: File, path: testdata/sB_2.fq}]
samples: [sampleA, sampleB]
expand_xa: true
```

## 7. Errors that validate but break

| Symptom | Cause | Fix |
|---|---|---|
| `is not a list, expected list of File` | job gives a map keyed by sample for a `File[]` input | YAML list, one `- class: File` per item |
| missing secondary file at run time | workflow input lacks `secondaryFiles` | declare them on the workflow input (section 5) |
| "Source ... may be incompatible with sink" | type or secondaryFiles differ | match types; copy secondaryFiles exactly |
| outputs overwrite each other | scattered tool writes a default name | set the name per shard with `valueFrom` |
| step input ignored | `in` key misspelled (`alignmnts`) | use the tool's input id |
| required input missing at run time | tool input not connected, no default | connect it or set step `default` |
| `pickValue is used but only a single input source is declared` | `pickValue` on one source | remove it; use an optional sink type |
| expected `.idx`, got `.tbi` | output named `.vcf.gz` but secondaryFiles say `.idx` | name `.vcf` for `.idx`, or declare `.tbi` |
| File[] into File | a scattered step's output feeds an unscattered step | scatter the next step too, or use an array input |
