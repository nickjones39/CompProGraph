# CompProGraph

[![DOI](https://zenodo.org/badge/1257770913.svg)](https://doi.org/10.5281/zenodo.20651569)

**Lossless structural compression for PROV-JSON provenance corpora.**

CompProGraph removes the redundancy that is endemic to machine-generated
[PROV-JSON](https://www.w3.org/Submission/prov-json/) provenance graphs —
repeated identifiers, structurally identical node records, and verbosely
repeated relation keys — while keeping the result **exactly reversible**.
Reconstruction is verified by a canonical-JSON round trip (object member order
is not semantically meaningful in PROV-JSON, so canonical equality is the
definition of losslessness used throughout).

It is evaluated on two corpora with **one codec and no per-corpus tuning**:

- **AgentDojo-PROV** — *real* LLM-agent tool-use provenance (17,664 graphs across
  six models: DeepSeek-Chat, GPT-5-Nano, Gemini-2.5-Flash, Llama-3.3-70B,
  Qwen-2.5-72B, GLM-4.6): **≈68% size reduction** (66.6–68.6% per model; 67.6%
  pooled), **verified lossless, 0 round-trip failures**.
- **OpenML-CC18** — machine-learning provenance used to stress-test scaling:
  **≈72% size reduction** (72.7% on the *light* corpus, 72.1–72.4% across the
  larger corpora, holding to 2.5 GB), **verified lossless, 0 failures**.

In both cases this is versus the original pretty-printed JSON. For reference,
simply minifying the JSON (stripping whitespace) reduces it by only ~16–18%, so
the bulk of the saving is genuine structural de-duplication, not free whitespace
removal.

> **Scope / honesty note.** The reported figure is the **structural stage only**;
> no general-purpose entropy coder (gzip/zstd/xz/brotli) is included in the
> reported number. The structural output is still a queryable PROV-JSON-shaped
> artifact and is *complementary* to entropy coding — applying `zstd`/`xz` on top
> pushes total lossless reduction to ~96–97% on the light corpus. That headroom
> is documented as a limitation, not claimed as part of the structural result.

---

## Related repositories

| Repository | Contents |
|------------|----------|
| **`nickjones39/CompProGraph`** (this repo, public) | The **codec and the evaluation harness**: compress, decompress, verify, round-trip tests, and the benchmark that produces the reported numbers. |
| **`nickjones39/CompProGraph-manuscript`** (private) | The **paper and its toolchain**: the IEEE TAI manuscript (`TAI_template.tex`, `references.bib`, figures) and the figure scripts (`make_figures.py`, `make_concept_figures.py`). |

The division of labour is deliberate: every benchmark number quoted in the paper
is produced *here*, by `benchmark_compprograph.py`; the manuscript repo only
consumes the `benchmark_results.json` this harness emits and renders it. See
[Corpora](#corpora) for the separately released datasets.

---

## How it works

Three lossless transforms are combined into a single codec
(`compprograph_compress.py`):

1. **Corpus-wide string dictionary.** Every JSON string — node identifiers,
   activity/entity references, role names, type URIs — *and every object key* is
   interned once in a global table and replaced by a small integer id. PROV-JSON
   repeats the same long `urn:`/`prov:` tokens thousands of times, so this is
   where most of the saving comes from.
2. **Supernode templates (Algorithm 1).** Each node record is split into a
   structural **template** (non-volatile attributes) and a per-node **residual**
   (volatile attributes: timestamps, checksums, labels, sizes). Identical
   templates are interned once, globally; a node stores only a template id plus
   its residual. Reconstruction is `record = {**template, **residual}`.
3. **Columnar edge encoding.** Each relation section (`used`, `wasGeneratedBy`,
   …) is stored column-wise — the edge ids, then one column per schema field,
   then any off-schema extras — so the repeated `prov:activity` / `prov:entity` /
   `prov:role` keys are paid for once, not per edge. The schema covers the **full
   set of W3C PROV-DM relation types**, a column cell may hold a plain string
   reference *or* a typed literal `{"$": v, "type": t}` (or any other JSON value),
   and **bundles** (nested documents) are compressed recursively.

Edges are ~63% of corpus bytes, so compressing them (not just node sections) is
what takes the codec from ~18% to ~72%.

Anything the transforms don't recognise falls back to a generic, still-lossless
dictionary encoding, so any valid PROV-JSON document round-trips exactly — see
[Compatibility](#compatibility) below.

---

## Repository layout

| Path | Purpose |
|------|---------|
| `compprograph_compress.py` | The codec. Importable **library API** + a `compress`/`decompress`/`verify` CLI. |
| `benchmark_compprograph.py` | Reproducible benchmarking suite (% reduction, runtime, memory, losslessness; emits JSON + LaTeX tables + figures). |
| `test_generic_provjson.py` | Round-trip tests on spec-style W3C PROV-JSON (typed literals, all relation types, bundles) + an OpenML non-regression check. |
| `LICENSE`, `CITATION.cff`, `.zenodo.json` | Software-release metadata for the archived version (see [Citation](#citation)). |
| `corpus-prov/<model>/` | **AgentDojo-PROV** — real LLM-agent provenance, six models (`prov-deepseek-chat`, `prov-gpt-5-nano`, `google_gemini-2.5-flash`, `meta-llama_llama-3.3-70b-instruct`, `qwen_qwen-2.5-72b-instruct`, `z-ai_glm-4.6`; 2,944 PROV graphs each under `prov/`). Each graph ships next to a `*.transcript.json` and a `manifest.json` that are **not** PROV-JSON and are skipped automatically (see below). *Local only — see [Corpora](#corpora).* |
| `prov_corpus_light/` | Small OpenML sample corpus (72 PROV graphs, one per CC18 task) — good for quick tests. *Local only.* |
| `prov_corpus_scaled/`, `prov_corpus_large/`, `prov_corpus_full/` | Larger OpenML corpora for scaling experiments. *Local only.* |
| `results/` | Benchmark output (`benchmark_results.json`, LaTeX tables, figures). *Generated, not tracked.* |

### Corpora

**The corpora are present in a local working copy but are not distributed via
git.** `corpus-prov/`, `prov_corpus_*/` and `results/` are gitignored: together
they run to roughly **4.6 GB**, far past what belongs in a repository. Clone this
repo, then obtain the data separately — each corpus has its own release:

| Corpus | Source |
|--------|--------|
| **AgentDojo-PROV** → `corpus-prov/<model>/` | Released separately as a Zenodo data release — [10.5281/zenodo.20955507](https://doi.org/10.5281/zenodo.20955507) (`jones2026agentdojoprov`), repo [`nickjones39/agentdojo-prov`](https://github.com/nickjones39/agentdojo-prov). |
| **OpenML-CC18** → `prov_corpus_{light,scaled,large,full}/` | Exported from OpenML-CC18 runs by **openml-to-prov**, released separately on Zenodo — [10.5281/zenodo.20470438](https://doi.org/10.5281/zenodo.20470438) (`jones2026openmltoprov`), repo [`nickjones39/openml-to-prov`](https://github.com/nickjones39/openml-to-prov). |

Nothing in the codec or the benchmark is specific to these two datasets: both
take a directory of PROV-JSON documents, so you can point them at your own
corpus and get the same guarantees (the *ratio* will depend on your data — see
[Compatibility](#compatibility)).

For OpenML development work use `prov_corpus_light` (72 graphs). The
`prov_corpus_{scaled,large,full}` corpora exist only for the scaling experiments
and are slow to run; the six AgentDojo-PROV corpora are fast enough to benchmark
directly.

---

## Requirements

- **Python 3.8+** (developed on 3.10). The codec itself uses only the standard
  library — **no third-party dependencies** for compression/decompression.
- **Optional**, only for the figures the benchmark emits: `matplotlib` and
  `numpy`. Pass `--skip-figures` and you need neither.

```bash
# Core codec + benchmark numbers: nothing to install.
# Only if you want the benchmark figures:
pip install matplotlib numpy
```

---

## Library / API usage

`compprograph_compress` is a clean, filesystem-free library so you can embed the
codec in other projects. Import it and work directly on parsed JSON (Python
dicts) — both the compressed *views* and the *model* are plain JSON-serialisable
structures you can persist however you like.

### 1. One self-contained document

The simplest entry point. The returned package round-trips entirely on its own
(it embeds the minimal model the document needs):

```python
import json
import compprograph_compress as cpg

doc = json.load(open("graph.prov.json"))          # a parsed PROV-JSON document

package = cpg.compress_document(doc)               # JSON-serialisable dict
json.dump(package, open("graph.cpg.json", "w"))    # persist anywhere

restored = cpg.decompress_document(package)        # exact inverse
assert cpg.canonical(restored) == cpg.canonical(doc)
```

### 2. Many documents sharing one model (recommended for corpora)

This is where cross-document de-duplication happens: structure discovered in one
document is reused by the next. Compress everything against one
`CompProGraphCodec`, then persist the per-document **views** alongside the single
shared **model**.

```python
import compprograph_compress as cpg

codec = cpg.CompProGraphCodec()
views = {name: codec.compress(doc) for name, doc in docs.items()}   # docs: dict[str, dict]
model = codec.dump_model()        # the shared string dict + supernode templates

# ... persist `views` (per document) and `model` (once) ...

# Later, in any process:
codec2 = cpg.CompProGraphCodec.from_model(model)
restored = {name: codec2.decompress(view) for name, view in views.items()}
```

> The views are only reconstructable together with the model **as it stands
> after the last `compress()`** — call `dump_model()` once, at the end.

### Persisting the model

```python
codec.dump_model()                 # -> dict (JSON-serialisable)
codec.dump_model_json(indent=2)    # -> pretty JSON string (omit indent for compact)
cpg.CompProGraphCodec.from_model(model_dict)
cpg.CompProGraphCodec.from_model_json(model_json_str)
```

### Whole-directory helpers

For corpus-on-disk workflows the same engine is exposed as functions that mirror
the CLI:

```python
cpg.compress_directory(input_dir, out_dir, verify=False)   # -> {"agg": {...}, ...}
cpg.decompress_directory(compressed_dir, out_dir)          # -> number of files restored
cpg.verify_directory(orig_dir, compressed_dir)             # -> {"lossless": bool, ...}
```

### Public API reference

| Symbol | What it does |
|--------|--------------|
| `compress_document(doc)` / `decompress_document(pkg)` | Self-contained single-document round trip. |
| `CompProGraphCodec()` | Stateful codec holding a shared model across many documents. |
| `.compress(doc)` → view / `.decompress(view)` → doc | In-memory encode / decode. |
| `.dump_model()` / `.dump_model_json(indent=…)` | Serialise the shared model. |
| `CompProGraphCodec.from_model(m)` / `.from_model_json(s)` | Rebuild a codec from a saved model. |
| `compress_directory` / `decompress_directory` / `verify_directory` | Corpus-on-disk operations. |
| `canonical(obj)` | Canonical-JSON string (the losslessness comparison key). |

---

## Command-line usage

`compprograph_compress.py` doubles as a CLI for whole-corpus operations:

```bash
# Compress a corpus directory (mirrors the input tree under out_dir/).
# --verify runs an honest canonical round-trip of every document and can fail.
python compprograph_compress.py compress \
    --input prov_corpus_light --out out_light --verify

# Reconstruct the originals from a compressed corpus (no access to the originals).
python compprograph_compress.py decompress \
    --input out_light --out reconstructed_light

# Independently verify a compressed corpus reconstructs the originals exactly.
python compprograph_compress.py verify \
    --orig prov_corpus_light --compressed out_light
```

A compressed corpus directory contains:

```
out_light/
  compressed_corpus.json        # global string dict + supernode templates + schema
  compressed_graphs/<relpath>   # one compressed view per input document
  benchmarks.csv                # per-graph redundancy stats
  decompressed_sanity.jsonl     # per-document round-trip log (with --verify)
```

---

## Benchmark usage

`benchmark_compprograph.py` is the reproducible evaluation harness. For each
corpus it reports the percentage reduction vs. the original (the headline), vs.
the minified-JSON reference baseline, runtime, peak memory, and a **two-way
losslessness check** (an in-process round trip *and* an independent full
decompression compared against the originals). It writes `benchmark_results.json`,
LaTeX tables, and (unless skipped) figures.

```bash
# Benchmark a single corpus:
python benchmark_compprograph.py --corpus prov_corpus_light --output results/

# Benchmark several corpora at once (repeat --corpus):
python benchmark_compprograph.py \
    --corpus prov_corpus_light --corpus prov_corpus_scaled --output results/

# Auto-discover prov_corpus_{light,scaled,large,full} under a parent directory:
python benchmark_compprograph.py --corpus-dir . --output results/

# Skip figures (no matplotlib/numpy needed):
python benchmark_compprograph.py --corpus prov_corpus_light --output results/ --skip-figures

# AgentDojo-PROV (real LLM provenance): all six models + a pooled total row.
# Transcripts (*.transcript.json) and manifest.json are skipped automatically;
# only the PROV graphs are compressed.
python benchmark_compprograph.py \
    --corpus corpus-prov/prov-deepseek-chat \
    --corpus corpus-prov/prov-gpt-5-nano \
    --corpus corpus-prov/google_gemini-2.5-flash \
    --corpus corpus-prov/meta-llama_llama-3.3-70b-instruct \
    --corpus corpus-prov/qwen_qwen-2.5-72b-instruct \
    --corpus corpus-prov/z-ai_glm-4.6 \
    --pooled-name "AgentDojo-PROV (pooled)" --output results/llm --skip-figures
```

> **Non-PROV sidecars.** A corpus directory may contain files that are valid JSON
> but not PROV-JSON documents. AgentDojo-PROV ships a `*.transcript.json` next to
> every graph plus a `manifest.json` and a `checksums.sha256`; OpenML-CC18 ships a
> `corpus_manifest.json` at the corpus root (all four sizes) and a
> `conformance_report.json` (light corpus). All are excluded from corpus discovery
> by `compprograph_compress.is_corpus_prov_file()`, used by both the codec and the
> benchmark, so byte counts and the compressed set always agree.
>
> Including one by mistake does not break losslessness — the generic fallback
> round-trips a manifest exactly like any other JSON object — but it does distort
> the measurement, because a one-off manifest has no redundancy to share and
> *expands* under the codec. If you add a corpus, enumerate its non-graph JSON
> files and extend `NON_PROV_BASENAMES`.

Useful flags: `--output DIR` (results/tables/figures destination), `--work-dir
DIR` (intermediate files; default is a temp dir), `--skip-figures`.

**Exit status.** The harness exits **non-zero if any requested corpus fails** —
whether it crashed, was named with `--corpus` but does not exist, or ran but did
not reconstruct exactly. It still writes `benchmark_results.json` (a partial file
is useful for diagnosis) but marks it `"complete": false` and lists what went
wrong under `failed_corpora`, and it refuses to emit a `--pooled-name` row, since
pooling the survivors would understate the corpus while carrying the full
corpus's label. **Check the exit code before using a results file**: a run that
lost a corpus produces a shorter file that otherwise looks entirely normal, which
is how a figure ends up silently missing rows.

> **Windows: keep `--work-dir` short.** Compression mirrors the input tree under
> `<work-dir>/<corpus>_struct/compressed_graphs/`, so the output path is the input's
> relative path plus ~43 characters. The deepest relative path in the OpenML corpora
> is 82 characters (`prov_corpus_full`), which leaves **135 characters** for the
> work-dir before hitting Windows' 260-character `MAX_PATH` — at which point the run
> dies with `FileNotFoundError` partway through, not with a clear error. The default
> temp dir is short enough; a hand-picked nested one may not be. Either pass
> something like `--work-dir C:/cpgbench` or enable `LongPathsEnabled`. Not an issue
> on Linux/macOS.

Expected output (light corpus):

```
Mode        Input(MB)  Struct(MB)  Reduction%  vsMinify%  Lossless
Light            2.37       0.647        72.7       66.5       yes
```

The **manuscript** figures are not generated here: they are produced in the
companion manuscript repo by `make_figures.py`, from the `benchmark_results.json`
this harness writes (see [Related repositories](#related-repositories)).

---

## Losslessness guarantee

Reconstruction is **exact** under canonical JSON (`json.dumps(sort_keys=True)`).
`decompress_doc` / `decompress_document` rebuild a document from *(compressed
view + model)* **alone** — they never see the original. Losslessness is checked
two independent ways: an in-process canonical round trip during compression, and
an independent full decompression of the persisted artifact compared against the
originals. Across all benchmarked corpora this reports **0 round-trip failures**.

"Lossless" here means **canonical-JSON** equality (`sort_keys=True`): object
member *order* and insignificant whitespace are not preserved, since they are not
semantically meaningful in PROV-JSON. Every key, value, and structure is.

---

## Compatibility

The codec works on **any W3C PROV-JSON document** — and, more generally, any JSON
**object** — and always round-trips it exactly. The specialised transforms cover:

- all node sections (`entity`, `activity`, `agent`);
- the **full set of W3C PROV-DM relations** (`used`, `wasGeneratedBy`,
  `wasAssociatedWith`, `wasInformedBy`, `wasAttributedTo`, `wasDerivedFrom`,
  `actedOnBehalfOf`, `wasStartedBy`, `wasEndedBy`, `wasInvalidatedBy`,
  `wasInfluencedBy`, `specializationOf`, `alternateOf`, `hadMember`, `mentionOf`);
- **typed-literal** field values (`{"$": v, "type": "xsd:…"}`), numbers, bools,
  and other JSON values, not just plain strings;
- **bundles** (named nested documents), compressed recursively.

Anything outside this set — an unusual extension relation, an off-schema
attribute, a vendor-specific structure — is handled by the generic fallback:
still lossless, just compressed less aggressively. The exact relation schema used
to encode a corpus is stored inside the artifact, so artifacts keep decoding even
if the schema table later changes.

> **On the compression *ratio*.** The two headline figures — ~72% on OpenML-CC18,
> ~68% on AgentDojo-PROV — come from **the same codec with no per-corpus tuning**,
> and each is specific to that corpus's redundancy. OpenML-CC18 is the corpus the
> codec was *developed against* (plain-string edge fields, a fixed attribute set),
> which is why it compresses best; AgentDojo-PROV was added later and run
> unchanged. Other PROV-JSON will still compress — primarily via the string
> dictionary and node templates — but expect a different ratio. Correctness
> (losslessness) does not vary.
>
> "No per-corpus tuning" is a measured claim, not a slogan: specialising
> `VOLATILE_ATTR_KEYS` *for* AgentDojo — moving its per-instance `adprov:`
> attributes (`content_hash`, `content_len`, `args`) into the residual set —
> **lowers** the pooled ratio from 67.6% to 66.2%, because full-record template
> interning already de-duplicates them while residuals are paid per node. The
> generic codec wins, so it is left generic.

The unit of work is a PROV-JSON *document*, which per the spec is always a JSON
object; a bare top-level scalar or array is out of scope.

---

## Tests

`test_generic_provjson.py` round-trips spec-style W3C PROV-JSON exercising typed
literals, the full relation set, present-vs-absent and null fields, off-schema
extras, marker-name collisions, and bundles — through **both** API paths — and
includes an OpenML non-regression check. It needs no test framework, and no
corpus on disk:

```bash
python test_generic_provjson.py      # prints PASS/FAIL per test; exit 0 on success
# or, if you have pytest:
pytest test_generic_provjson.py
```

To re-confirm the published numbers and end-to-end losslessness, run the
benchmark on the OpenML *light* corpus (72 graphs; obtain it as described under
[Corpora](#corpora)):

```bash
python benchmark_compprograph.py --corpus prov_corpus_light --output results/ --skip-figures
```

---

## Citation

This code accompanies an IEEE Transactions on Artificial Intelligence manuscript
on PROV-JSON provenance graph compression. Please cite the paper if you use
CompProGraph in your work. To cite the software itself, see `CITATION.cff` (the
archived version is on Zenodo — the DOI badge at the top of this file).
