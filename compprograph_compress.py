#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CompProGraph — LOSSLESS structural compression for PROV-JSON corpora.

This is Stage 1 of the pipeline. It realises Algorithm 1 (structural-signature
supernodes) as an *invertible* codec and -- crucially -- compresses BOTH the
node sections AND the edge (relation) sections of every document. In the
OpenML-CC18 provenance corpora the edge sections are ~63% of all bytes, so a
codec that only touches node records (the previous behaviour) leaves most of the
redundancy on the table. Three lossless transforms are combined:

  1. CORPUS-WIDE STRING DICTIONARY.  Every JSON string -- node identifiers,
     activity/entity references, role names, type URIs, prefix values, and the
     object keys themselves -- is interned once in a global table and replaced
     by a small integer id. PROV-JSON repeats the same long urn:/prov: strings
     thousands of times across a corpus, so this is where most of the saving
     comes from. The dictionary is fully reversible.

  2. SUPERNODE TEMPLATES (Algorithm 1).  Node records (entity/activity/agent)
     are split into a structural TEMPLATE (the non-volatile attributes) and a
     per-node RESIDUAL (volatile attributes: timestamps, checksums, labels,
     sizes). Identical templates are interned once, globally, in the supernode
     table; each node stores only a template id plus its residual. Reconstruction
     is record = {**template, **residual}.

     Note vs. Algorithm 1 as printed: the paper's signature also folds in the
     in/out degree profile. For a *lossless* codec that profile is fully
     recoverable from the (preserved, and here also compressed) edge sections,
     so storing it per node would be pure redundancy. We therefore intern
     templates by attribute content, which merges at least as aggressively as
     the degree-keyed signature while never storing a derivable quantity. The
     grouping is still exactly Algorithm 1's "structurally equivalent nodes".

  3. COLUMNAR EDGE ENCODING.  Each relation section (`used`, `wasGeneratedBy`,
     ...) is a list/dict of edge records that all share the same handful of keys
     (`prov:activity`, `prov:entity`, `prov:role`, ...). Instead of repeating
     those keys on every edge, the section is stored column-wise: the edge ids,
     then one column per schema field, then any off-schema extras. The repeated
     keys are paid for once (in the schema), not per edge. The schema covers the
     full set of W3C PROV-DM relation types, and a column cell may hold a plain
     string reference (stored compactly as a dictionary id) OR a typed literal
     `{"$": v, "type": t}` / any other JSON value -- so spec-compliant PROV-JSON
     (which uses typed literals for times and values) compresses columnar too.

Bundles (named nested documents under the top-level `bundle` key) are compressed
RECURSIVELY through the same shared string dictionary and supernode registry, so
their nodes and edges benefit from the same three transforms.

Relation types or attributes the transforms do not recognise fall back to a
generic, still-lossless dictionary encoding, so ANY valid PROV-JSON document --
indeed any JSON object -- round-trips exactly; the transforms only affect how
much is saved. (The codec's unit of work is a PROV-JSON *document*, which per the
spec is always a JSON object. A bare top-level STRING is rejected with ValueError,
because its encoding {"$": id} is indistinguishable from the view of the object
document {"$": id}; other bare top-level values round-trip but are out of spec.)

Reconstruction is exact and is checked on CANONICAL JSON
(`json.dumps(sort_keys=True)`); JSON object member order is not semantically
meaningful in PROV-JSON, so canonical equality is the standard definition of
losslessness used here. `decompress_doc` rebuilds a document from
(compressed view + global tables) ALONE -- it has no access to the original.

Outputs (out_dir/):
    compressed_corpus.json        # global string dict + supernode templates + schema
    compressed_graphs/<relpath>   # per-document compressed view (mirrors input tree)
    benchmarks.csv                # per-graph node/edge/supernode redundancy stats
    decompressed_sanity.jsonl     # if --verify: honest per-doc canonical round-trip

-----------------------------------------------------------------------------
LIBRARY / API USAGE
-----------------------------------------------------------------------------
This module is also a clean, importable library so other projects can embed the
codec without going through the filesystem. The two entry points are:

    import compprograph_compress as cpg

    # (a) One self-contained document (round-trips on its own):
    package = cpg.compress_document(prov_json_dict)   # JSON-serialisable
    restored = cpg.decompress_document(package)        # == prov_json_dict (canonically)

    # (b) Many documents sharing one model (deduplicates across the corpus):
    codec = cpg.CompProGraphCodec()
    views = {name: codec.compress(doc) for name, doc in docs.items()}
    model = codec.dump_model()                         # the shared tables
    #   ... persist `views` + `model`, then later ...
    codec2 = cpg.CompProGraphCodec.from_model(model)
    docs2 = {name: codec2.decompress(view) for name, view in views.items()}

Directory-level helpers (`compress_directory`, `decompress_directory`,
`verify_directory`) mirror the CLI for whole-corpus workflows. See README.md.

CLI:
    compress   --input DIR --out OUTDIR [--verify]
    decompress --input OUTDIR --out RECONSTRUCTED_DIR
    verify     --orig DIR --compressed OUTDIR
"""
import json, os, argparse, time, glob, shutil, tempfile
from typing import Dict, List, Tuple, Any, Optional, Set

__version__ = "1.0.0"

# Public API surface (see "LIBRARY / API USAGE" above). Everything else is an
# implementation detail and may change without notice.
__all__ = [
    "__version__",
    # High-level codec (stateful, shared model across documents)
    "CompProGraphCodec",
    # One-shot, self-contained single-document helpers
    "compress_document", "decompress_document",
    # Directory / corpus helpers
    "compress_directory", "decompress_directory", "verify_directory",
    "run_corpus", "decompress_corpus", "verify_corpus",
    # Low-level building blocks
    "compress_doc", "decompress_doc", "canonical",
    "StringDict", "SupernodeRegistry",
    # Schema constants
    "NODE_SECTIONS", "REL_SCHEMA", "VOLATILE_ATTR_KEYS",
]


# =============================================================================
# Canonicalization helper
# =============================================================================

def canonical(obj: Any) -> str:
    """Canonical JSON string used as the structural signature (template key) and
    for round-trip equality.

    Sorting keys makes the representation independent of object member order,
    which is the (standard) definition of losslessness we guarantee for
    PROV-JSON documents. Using the exact canonical form as the supernode key
    makes template grouping collision-free (no cryptographic hash is required).
    """
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


# =============================================================================
# PROV-JSON structure constants
# =============================================================================

# Node sections (records keyed by node id).
NODE_SECTIONS = ("entity", "activity", "agent")

# Edge/relation sections and the ordered field schema we columnarise. This covers
# the full set of W3C PROV-DM relation types (PROV-JSON member names). Any field
# present on an edge but NOT in its schema is preserved verbatim in the per-edge
# "extra" slot, so the schema can be conservative without risking losslessness --
# and any relation type NOT listed here is handled by the generic (still lossless)
# fallback. The exact schema used to encode a corpus is persisted in the artifact
# (compressed_corpus.json -> "rel_schema"), so decoding never depends on this
# in-memory copy and old artifacts keep decoding even if this table later grows.
REL_SCHEMA: Dict[str, List[str]] = {
    "used":              ["prov:activity", "prov:entity", "prov:role", "prov:time"],
    "wasGeneratedBy":    ["prov:entity", "prov:activity", "prov:role", "prov:time"],
    "wasAssociatedWith": ["prov:activity", "prov:agent", "prov:plan", "prov:role"],
    "wasInformedBy":     ["prov:informed", "prov:informant"],
    "wasAttributedTo":   ["prov:entity", "prov:agent", "prov:role"],
    "wasDerivedFrom":    ["prov:generatedEntity", "prov:usedEntity",
                          "prov:activity", "prov:generation", "prov:usage"],
    "actedOnBehalfOf":   ["prov:delegate", "prov:responsible", "prov:activity"],
    # --- remaining W3C PROV-DM relations ---
    "wasStartedBy":      ["prov:activity", "prov:trigger", "prov:starter", "prov:time"],
    "wasEndedBy":        ["prov:activity", "prov:trigger", "prov:ender", "prov:time"],
    "wasInvalidatedBy":  ["prov:entity", "prov:activity", "prov:role", "prov:time"],
    "wasInfluencedBy":   ["prov:influencee", "prov:influencer"],
    "specializationOf":  ["prov:specificEntity", "prov:generalEntity"],
    "alternateOf":       ["prov:alternate1", "prov:alternate2"],
    "hadMember":         ["prov:collection", "prov:entity"],
    "mentionOf":         ["prov:specificEntity", "prov:generalEntity", "prov:bundle"],
}

# Volatile / instance-specific attribute keys. EXCLUDED from the shared template
# and carried per node as the residual. Preserved exactly -- just not shared.
VOLATILE_ATTR_KEYS = {
    "prov:label", "prov:location", "checksum:sha256", "model:path",
    "pred:rows", "pred:folds", "split:size_bytes", "pred:size_bytes", "model:size_bytes",
    "openml:task_id", "flowparams:sha256", "prov:value", "prov:generatedAtTime",
    "prov:startTime", "prov:endTime", "prov:atTime",
}
# Backwards-compatible alias.
IGNORED_ATTR_KEYS = VOLATILE_ATTR_KEYS

# View markers. Underscore-prefixed so they never collide with dictionary-encoded
# object keys (which are always "#<id>") or string leaves (always {"$": <id>}).
NODE_MARKER = "_n"   # value: [ids, template_refs, residuals]
EDGE_MARKER = "_e"   # value: [edge_ids, columns, extras]
BUNDLE_MARKER = "_b" # value: [bundle_ids, bundle_views]  (each view recursively encoded)
STR_KEY = "$"        # string leaf:  {"$": <dict id>}


# =============================================================================
# Global string dictionary
# =============================================================================

class StringDict:
    """Corpus-wide intern table: string -> small integer id (its index)."""

    def __init__(self):
        self._index: Dict[str, int] = {}
        self.table: List[str] = []

    def intern(self, s: str) -> int:
        i = self._index.get(s)
        if i is None:
            i = len(self.table)
            self._index[s] = i
            self.table.append(s)
        return i


def enc(v: Any, D: StringDict) -> Any:
    """Dictionary-encode an arbitrary JSON value (reversible by `dec`).

      str  -> {"$": id}
      dict -> {"#"+id: enc(child)}      (keys interned too)
      list -> [enc(x), ...]
      int/float/bool/null -> verbatim
    """
    if isinstance(v, str):
        return {STR_KEY: D.intern(v)}
    if isinstance(v, dict):
        return {("#" + str(D.intern(k))): enc(val, D) for k, val in v.items()}
    if isinstance(v, list):
        return [enc(x, D) for x in v]
    return v


def dec(v: Any, T: List[str]) -> Any:
    """Inverse of `enc`, given the resolved string table `T`."""
    if isinstance(v, dict):
        if len(v) == 1 and STR_KEY in v:
            return T[v[STR_KEY]]
        return {T[int(k[1:])]: dec(val, T) for k, val in v.items()}
    if isinstance(v, list):
        return [dec(x, T) for x in v]
    return v


# =============================================================================
# Supernode (template) registry
# =============================================================================

class SupernodeRegistry:
    """Global table of structural templates (the shared, non-volatile part of
    node records). Interned by template content; a template's id is its index."""

    def __init__(self, D: StringDict):
        self._D = D
        self._index: Dict[str, int] = {}     # canonical(template) -> template id
        self.templates: List[Any] = []        # id -> enc(template)

    def intern(self, template: Dict[str, Any]) -> int:
        key = canonical(template)
        i = self._index.get(key)
        if i is None:
            i = len(self.templates)
            self._index[key] = i
            self.templates.append(enc(template, self._D))
        return i


def split_record(record: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Partition a node record into (template, residual) by key.

    template = structural (non-volatile) attributes -> shared via supernode.
    residual = volatile attributes                  -> stored per node.

    The key sets are disjoint and their union is the original record, so
    {**template, **residual} reconstructs the record exactly.
    """
    template: Dict[str, Any] = {}
    residual: Dict[str, Any] = {}
    for k, v in record.items():
        (residual if k in VOLATILE_ATTR_KEYS else template)[k] = v
    return template, residual


# =============================================================================
# Per-document compression
# =============================================================================

def _encode_node_section(section: Dict[str, Any], D: StringDict,
                         reg: SupernodeRegistry) -> Tuple[Any, int, int, Set[int]]:
    """Encode an entity/activity/agent section. Returns (encoded, n_nodes,
    n_residual, used_template_ids). Falls back to a generic encoding (returns
    None as `encoded`) if any record is not a JSON object."""
    if not all(isinstance(rec, dict) for rec in section.values()):
        return None, 0, 0, set()
    ids: List[int] = []
    refs: List[int] = []
    res: List[Any] = []
    used: Set[int] = set()
    n_residual = 0
    for nid, record in section.items():
        ids.append(D.intern(nid))
        template, residual = split_record(record)
        tid = reg.intern(template)
        refs.append(tid)
        used.add(tid)
        if residual:
            res.append(enc(residual, D))
            n_residual += 1
        else:
            res.append(0)
    return {NODE_MARKER: [ids, refs, res]}, len(ids), n_residual, used


def _encode_edge_section(rel: str, section: Dict[str, Any], D: StringDict) -> Optional[Any]:
    """Encode a relation section column-wise. Returns None (caller falls back to a
    generic encoding) unless every edge is a JSON object.

    Each schema-field column cell uses a 3-way, unambiguous, fully reversible
    encoding so that *any* field value -- not just plain strings -- is columnar-safe:

        absent field          -> null            (the only null a cell can be)
        present string value  -> int             (its string-dictionary id; compact)
        present non-string    -> [enc(value)]    (one-element list; covers typed
                                                  literals {"$":v,"type":t}, numbers,
                                                  bools, JSON null, nested objects)

    These three forms are disjoint Python/JSON types (None / int / list), so the
    decoder never has to guess. The bare-int fast path keeps the common
    string-valued PROV reference fields exactly as compact as before (no size
    regression on string-only corpora), while typed literals -- pervasive in
    spec-compliant PROV-JSON for time/typed values -- now compress columnar too
    instead of forcing the whole section to the generic fallback.
    """
    schema = REL_SCHEMA[rel]
    eids: List[int] = []
    cols: List[List[Any]] = [[] for _ in schema]
    extras: List[Any] = []
    for eid, ev in section.items():
        if not isinstance(ev, dict):
            return None
        eids.append(D.intern(eid))
        used_fields = set()
        for ci, fld in enumerate(schema):
            if fld in ev:
                val = ev[fld]
                if isinstance(val, str):
                    cols[ci].append(D.intern(val))     # compact: bare dict id
                else:
                    cols[ci].append([enc(val, D)])      # list-wrapped, never bare int/null
                used_fields.add(fld)
            else:
                cols[ci].append(None)                   # absent
        extra = {k: v for k, v in ev.items() if k not in used_fields}
        extras.append(enc(extra, D) if extra else 0)
    return {EDGE_MARKER: [eids, cols, extras]}


def count_edges(raw: Any) -> int:
    """Count edge entries across all relation sections, recursing into bundles
    (for stats only)."""
    if not isinstance(raw, dict):
        return 0
    n = 0
    for rel in REL_SCHEMA:
        v = raw.get(rel)
        if isinstance(v, (list, dict)):
            n += len(v)
    bundles = raw.get("bundle")
    if isinstance(bundles, dict):
        for nested in bundles.values():
            n += count_edges(nested)
    return n


def compress_doc(raw_doc: Any, D: StringDict,
                 reg: SupernodeRegistry) -> Tuple[Any, Set[int], int, int]:
    """Compress one raw PROV-JSON document into its encoded view.

    Returns (view, used_template_ids, n_nodes, n_residual_nodes). Node sections
    become {NODE_MARKER: ...}, edge sections {EDGE_MARKER: ...} when columnar-safe,
    bundle sections {BUNDLE_MARKER: ...} (each member document compressed
    RECURSIVELY through the same shared tables), and every other value (prefix,
    metadata) is dictionary-encoded.

    Raises ValueError for a bare top-level string: its encoding {"$": id} would
    be byte-identical to the view of the object document {"$": id}, so it cannot
    be reconstructed unambiguously. PROV-JSON documents are always JSON objects,
    so this never affects spec-compliant input.
    """
    if isinstance(raw_doc, str):
        raise ValueError(
            "compress_doc: a bare top-level JSON string is not a PROV-JSON "
            "document and cannot be encoded unambiguously; wrap it in an object.")
    if not isinstance(raw_doc, dict):
        return enc(raw_doc, D), set(), 0, 0

    view: Dict[str, Any] = {}
    used: Set[int] = set()
    n_nodes = 0
    n_residual = 0

    for key, val in raw_doc.items():
        if key in NODE_SECTIONS and isinstance(val, dict):
            encoded, nn, nr, u = _encode_node_section(val, D, reg)
            if encoded is not None:
                view[key] = encoded
                used |= u
                n_nodes += nn
                n_residual += nr
                continue
        elif key in REL_SCHEMA and isinstance(val, dict):
            encoded = _encode_edge_section(key, val, D)
            if encoded is not None:
                view[key] = encoded
                continue
        elif key == "bundle" and isinstance(val, dict) and \
                all(isinstance(b, dict) for b in val.values()):
            # Each bundle member is a named nested PROV document; compress it
            # recursively so its nodes/edges are templated/columnarised too, all
            # sharing this corpus's string dictionary and supernode registry.
            bids: List[int] = []
            bviews: List[Any] = []
            for bid, nested in val.items():
                bids.append(D.intern(bid))
                nview, nused, nn, nr = compress_doc(nested, D, reg)
                bviews.append(nview)
                used |= nused
                n_nodes += nn
                n_residual += nr
            view[key] = {BUNDLE_MARKER: [bids, bviews]}
            continue
        # Fallback / everything else: generic dictionary encoding.
        view[key] = enc(val, D)

    return view, used, n_nodes, n_residual


def decompress_doc(view: Any, ctx: Dict[str, Any]) -> Any:
    """Reconstruct a document from its compressed view and the global tables.

    `ctx` carries {"T": string_table, "templates": decoded_templates,
    "rel_schema": schema}. This is the REAL decompressor: it has no access to
    the original document.
    """
    T: List[str] = ctx["T"]
    templates: List[Any] = ctx["templates"]
    rel_schema: Dict[str, List[str]] = ctx["rel_schema"]

    if not isinstance(view, dict):
        return dec(view, T)

    doc: Dict[str, Any] = {}
    for key, val in view.items():
        if isinstance(val, dict) and NODE_MARKER in val and len(val) == 1:
            ids, refs, res = val[NODE_MARKER]
            section: Dict[str, Any] = {}
            for i, nid in enumerate(ids):
                record = dict(templates[refs[i]])   # decoded template (shared)
                r = res[i]
                if r != 0:
                    record.update(dec(r, T))
                section[T[nid]] = record
            doc[key] = section
        elif isinstance(val, dict) and EDGE_MARKER in val and len(val) == 1:
            eids, cols, extras = val[EDGE_MARKER]
            schema = rel_schema[key]
            section = {}
            for i, eid in enumerate(eids):
                ev: Dict[str, Any] = {}
                for ci, fld in enumerate(schema):
                    cell = cols[ci][i]
                    # Inverse of the 3-way cell encoding (see _encode_edge_section):
                    #   null -> absent;  list -> non-string value;  int -> string id.
                    if cell is None:
                        continue
                    if isinstance(cell, list):
                        ev[fld] = dec(cell[0], T)
                    else:
                        ev[fld] = T[cell]
                ex = extras[i]
                if ex != 0:
                    ev.update(dec(ex, T))
                section[T[eid]] = ev
            doc[key] = section
        elif isinstance(val, dict) and BUNDLE_MARKER in val and len(val) == 1:
            bids, bviews = val[BUNDLE_MARKER]
            section = {}
            for i, bid in enumerate(bids):
                section[T[bid]] = decompress_doc(bviews[i], ctx)  # recurse
            doc[key] = section
        else:
            doc[key] = dec(val, T)
    return doc


# =============================================================================
# Filesystem helpers
# =============================================================================

# Sidecar files that live inside a corpus tree but are NOT PROV-JSON documents
# and must be skipped during corpus discovery. The AgentDojo-PROV corpora ship a
# `manifest.json` index, a `checksums.sha256`, and a `<name>.transcript.json`
# (schema "transcript/v1") next to every PROV graph; only the PROV graphs are
# compressed. (OpenML-CC18 corpora contain none of these, so this is a no-op
# there.) Excluding by name keeps file discovery and the byte/round-trip
# accounting consistent everywhere they are used.
NON_PROV_BASENAMES = {"manifest.json", "checksums.sha256"}
NON_PROV_SUFFIXES = (".transcript.json",)


def is_corpus_prov_file(path: str) -> bool:
    """True unless `path` is a known non-PROV sidecar (transcript / manifest)."""
    base = os.path.basename(path)
    if base in NON_PROV_BASENAMES:
        return False
    return not base.endswith(NON_PROV_SUFFIXES)


def find_json_files_recursive(root_dir: str) -> List[str]:
    """Recursively find all PROV-JSON documents, skipping non-PROV sidecars."""
    files: List[str] = []
    for pattern in ('**/*.json', '**/*.prov.json'):
        files.extend(glob.glob(os.path.join(root_dir, pattern), recursive=True))
    return sorted(p for p in set(files) if is_corpus_prov_file(p))


def _load_global_tables(compressed_dir: str) -> Dict[str, Any]:
    """Load string dict + templates + schema and pre-decode the templates once."""
    with open(os.path.join(compressed_dir, "compressed_corpus.json"), "r", encoding="utf-8") as f:
        corpus = json.load(f)
    T = corpus["dict"]
    templates = [dec(t, T) for t in corpus["templates"]]
    rel_schema = corpus.get("rel_schema", REL_SCHEMA)
    return {"T": T, "templates": templates, "rel_schema": rel_schema}


# =============================================================================
# Decompression of a whole compressed corpus (standalone, runnable)
# =============================================================================

def decompress_corpus(compressed_dir: str, out_dir: str) -> int:
    """Reconstruct every original document from a compressed corpus directory.

    Reads `compressed_corpus.json` + `compressed_graphs/**` and writes the
    reconstructed documents to `out_dir`, mirroring the original tree. Returns
    the number of files reconstructed.
    """
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    ctx = _load_global_tables(compressed_dir)
    cg_dir = os.path.join(compressed_dir, "compressed_graphs")
    view_files = find_json_files_recursive(cg_dir)

    n = 0
    for vf in view_files:
        with open(vf, "r", encoding="utf-8") as f:
            view = json.load(f)
        rel = os.path.relpath(vf, cg_dir)
        doc = decompress_doc(view, ctx)
        outp = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(outp), exist_ok=True)
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, sort_keys=True, indent=2)
        n += 1
    return n


# =============================================================================
# Main compression pipeline
# =============================================================================

def run_corpus(input_dir: str, out_dir: str, verify: bool = False) -> Dict[str, Any]:
    """Compress an entire PROV-JSON corpus losslessly.

    1. Intern strings + node templates globally; encode nodes and edges per doc.
    2. Persist the global tables and the per-document views.
    3. If verify: reconstruct each document FROM THE PERSISTED ARTIFACT ALONE and
       compare canonically to the original (this check can fail).
    """
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "compressed_graphs"), exist_ok=True)

    file_paths = find_json_files_recursive(input_dir)
    total_files = len(file_paths)
    print(f"[INFO] Found {total_files} JSON files recursively")

    D = StringDict()
    reg = SupernodeRegistry(D)
    rows: List[Dict[str, Any]] = []
    processed: List[str] = []
    total_orig_nodes = 0
    total_orig_edges = 0
    total_residual_nodes = 0

    t0 = time.time()
    for i, fp in enumerate(file_paths):
        if (i + 1) % 1000 == 0 or (i + 1) == total_files:
            print(f"[INFO] Compressing file {i+1}/{total_files}...")
        rel = os.path.relpath(fp, input_dir)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[WARN] Skipping {rel}: {e}")
            continue

        try:
            view, used, n_nodes, n_res = compress_doc(raw, D, reg)
        except ValueError as e:
            print(f"[WARN] Skipping {rel}: {e}")
            continue
        n_edges = count_edges(raw)
        total_orig_nodes += n_nodes
        total_orig_edges += n_edges
        total_residual_nodes += n_res

        outp = os.path.join(out_dir, "compressed_graphs", rel)
        os.makedirs(os.path.dirname(outp), exist_ok=True)
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(view, f, separators=(',', ':'), ensure_ascii=False)
        processed.append(rel)

        rows.append({
            "graph": rel,
            "orig_nodes": n_nodes,
            "orig_edges": n_edges,
            "comp_nodes": len(used),       # distinct templates referenced locally
            "comp_edges": n_edges,
            "nodes_ratio": round(len(used) / max(1, n_nodes), 4),
            "edges_ratio": 1.0,
        })
    t1 = time.time()
    print(f"[INFO] Compressed {len(processed)} files: "
          f"{len(D.table):,} dict strings, {len(reg.templates):,} supernode templates "
          f"in {t1-t0:.2f}s")

    # Global tables, written compactly.
    compressed_corpus = {
        "dict": D.table,
        "templates": reg.templates,
        "rel_schema": REL_SCHEMA,
        "meta": {
            "graphs": len(processed),
            "dict_strings": len(D.table),
            "total_supernodes": len(reg.templates),
            "orig_nodes": total_orig_nodes,
            "orig_edges": total_orig_edges,
            "residual_nodes": total_residual_nodes,
            "created_at": time.time(),
            "compress_ms": int((t1 - t0) * 1000),
            "lossless_definition": "canonical-JSON (sort_keys) exact round-trip",
            "markers": {"node": NODE_MARKER, "edge": EDGE_MARKER, "string": STR_KEY},
        },
    }
    with open(os.path.join(out_dir, "compressed_corpus.json"), "w", encoding="utf-8") as f:
        json.dump(compressed_corpus, f, separators=(',', ':'), ensure_ascii=False)

    # Benchmarks CSV (redundancy stats; not the storage ratio).
    with open(os.path.join(out_dir, "benchmarks.csv"), "w", encoding="utf-8") as f:
        f.write("graph,orig_nodes,orig_edges,comp_nodes,comp_edges,nodes_ratio,edges_ratio\n")
        for r in rows:
            f.write(",".join(str(r[k]) for k in ["graph", "orig_nodes", "orig_edges", "comp_nodes", "comp_edges", "nodes_ratio", "edges_ratio"]) + "\n")

    # Honest verification: rebuild from the persisted artifact ONLY.
    failures = None
    if verify:
        print("[INFO] Verifying lossless round-trip from persisted artifact...")
        failures = 0
        ctx = _load_global_tables(out_dir)
        sanity_path = os.path.join(out_dir, "decompressed_sanity.jsonl")
        with open(sanity_path, "w", encoding="utf-8") as sf:
            for rel in processed:
                with open(os.path.join(out_dir, "compressed_graphs", rel), "r", encoding="utf-8") as f:
                    view = json.load(f)
                recon = decompress_doc(view, ctx)
                with open(os.path.join(input_dir, rel), "r", encoding="utf-8") as f:
                    original = json.load(f)
                ok = canonical(recon) == canonical(original)
                if not ok:
                    failures += 1
                sf.write(json.dumps({"graph": rel, "path": rel, "canonical_ok": ok}) + "\n")
        if failures == 0:
            print(f"[INFO] Round-trip OK: {len(processed)}/{len(processed)} documents reconstructed exactly.")
        else:
            print(f"[ERROR] LOSSY: {failures}/{len(processed)} documents failed canonical round-trip!")

    agg = {
        "graphs": len(processed),
        "total_supernodes": len(reg.templates),
        "dict_strings": len(D.table),
        "orig_nodes": total_orig_nodes,
        "orig_edges": total_orig_edges,
        "residual_nodes": total_residual_nodes,
        "node_dedup_ratio": round(len(reg.templates) / max(1, total_orig_nodes), 4),
        "lossless": (failures == 0) if failures is not None else None,
        "roundtrip_failures": failures,
    }

    print(f"\n[RESULTS]")
    print(f"  Documents:        {len(processed):,}")
    print(f"  Original nodes:   {total_orig_nodes:,} -> Supernode templates (global): {len(reg.templates):,} "
          f"(dedup ratio: {agg['node_dedup_ratio']:.4f})")
    print(f"  Edges encoded:    {total_orig_edges:,} (columnar, dictionary-coded)")
    print(f"  Dict strings:     {len(D.table):,}")
    if failures is not None:
        print(f"  Lossless:         {agg['lossless']} (round-trip failures: {failures})")

    return {"rows": rows, "agg": agg, "out_dir": out_dir}


# =============================================================================
# Standalone verification (original corpus vs compressed corpus)
# =============================================================================

def verify_corpus(orig_dir: str, compressed_dir: str) -> Dict[str, Any]:
    """Reconstruct from `compressed_dir` and compare canonically to `orig_dir`.

    Returns {"checked", "failures", "lossless", "mismatches": [...]}.
    """
    tmp = tempfile.mkdtemp(prefix="cpg_verify_")
    try:
        decompress_corpus(compressed_dir, tmp)
        orig_files = find_json_files_recursive(orig_dir)
        checked = 0
        failures = 0
        mismatches: List[str] = []
        for fp in orig_files:
            rel = os.path.relpath(fp, orig_dir)
            recon_path = os.path.join(tmp, rel)
            if not os.path.exists(recon_path):
                failures += 1
                mismatches.append(f"{rel} (no reconstruction produced)")
                continue
            with open(fp, "r", encoding="utf-8") as f:
                original = json.load(f)
            with open(recon_path, "r", encoding="utf-8") as f:
                recon = json.load(f)
            checked += 1
            if canonical(original) != canonical(recon):
                failures += 1
                mismatches.append(rel)
        return {
            "checked": checked,
            "failures": failures,
            "lossless": failures == 0,
            "mismatches": mismatches[:20],
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =============================================================================
# Public library API
# =============================================================================
#
# The functions above (run_corpus / decompress_corpus / verify_corpus and the
# compress_doc / decompress_doc / canonical primitives) are the on-disk engine
# and remain the stable contract the benchmark depends on. The wrappers below
# give other projects an ergonomic, filesystem-free way to embed the codec.


class CompProGraphCodec:
    """Stateful, lossless PROV-JSON codec for in-process / programmatic use.

    The codec owns a corpus-wide string dictionary and a supernode-template
    registry (the "model"). Compressing documents grows that shared model, so
    structure discovered in one document is reused by later ones -- this is
    where the cross-document de-duplication comes from. To reconstruct, a
    decoder needs the FINAL model plus each document's compressed view.

    Typical usage::

        codec = CompProGraphCodec()
        views = {name: codec.compress(doc) for name, doc in docs.items()}
        model = codec.dump_model()            # persist this alongside `views`

        codec2 = CompProGraphCodec.from_model(model)
        docs2 = {name: codec2.decompress(view) for name, view in views.items()}

    `compress()` and `decompress()` operate purely on Python objects (parsed
    JSON), never on files. Both the per-document `view`s and `dump_model()` are
    plain JSON-serialisable structures.
    """

    def __init__(self) -> None:
        self._D = StringDict()
        self._reg = SupernodeRegistry(self._D)
        self._rel_schema: Dict[str, List[str]] = REL_SCHEMA
        self._decode_ctx: Optional[Dict[str, Any]] = None  # lazily built cache

    # --- encoding (mutates the shared model) --------------------------------

    def compress(self, doc: Any) -> Any:
        """Compress one parsed PROV-JSON document into its compressed view.

        Interns the document's strings and node templates into the shared
        model (so the model may grow) and returns the document's view. The
        returned view is only reconstructable together with the model as it
        stands AFTER all documents you care about have been compressed -- call
        `dump_model()` once at the end.
        """
        view, _used, _n_nodes, _n_res = compress_doc(doc, self._D, self._reg)
        self._decode_ctx = None  # the model changed; invalidate the decode cache
        return view

    # --- model serialization ------------------------------------------------

    def dump_model(self) -> Dict[str, Any]:
        """Return the shared model (string dict + supernode templates + schema)
        as a JSON-serialisable dict. Pair this with the per-document views to
        reconstruct later via `from_model`."""
        return {
            "version": __version__,
            "dict": self._D.table,
            "templates": self._reg.templates,   # stored in encoded form
            "rel_schema": self._rel_schema,
        }

    def dump_model_json(self, *, indent: Optional[int] = None) -> str:
        """`dump_model()` serialised to a compact (or pretty, if `indent`) JSON
        string."""
        seps = (',', ':') if indent is None else None
        return json.dumps(self.dump_model(), ensure_ascii=False,
                          separators=seps, indent=indent)

    @classmethod
    def from_model(cls, model: Dict[str, Any]) -> "CompProGraphCodec":
        """Rebuild a codec from a `dump_model()` payload (or a parsed
        `dump_model_json()` string). The result can decompress views produced
        against that model, and can also keep compressing new documents on top
        of it."""
        codec = cls()
        table: List[str] = list(model["dict"])
        codec._D.table = table
        codec._D._index = {s: i for i, s in enumerate(table)}
        templates: List[Any] = list(model["templates"])
        codec._reg.templates = templates
        # The registry interns by canonical(decoded-template); rebuild that index
        # so further compression stays consistent with the loaded model.
        codec._reg._index = {canonical(dec(t, table)): i
                             for i, t in enumerate(templates)}
        codec._rel_schema = model.get("rel_schema", REL_SCHEMA)
        codec._decode_ctx = None
        return codec

    @classmethod
    def from_model_json(cls, model_json: str) -> "CompProGraphCodec":
        """Convenience: `from_model(json.loads(model_json))`."""
        return cls.from_model(json.loads(model_json))

    # --- decoding -----------------------------------------------------------

    def _ctx(self) -> Dict[str, Any]:
        if self._decode_ctx is None:
            T = self._D.table
            self._decode_ctx = {
                "T": T,
                "templates": [dec(t, T) for t in self._reg.templates],
                "rel_schema": self._rel_schema,
            }
        return self._decode_ctx

    def decompress(self, view: Any) -> Any:
        """Reconstruct a document from its compressed `view` using the codec's
        current model. Exact inverse of `compress` (canonical-JSON equal)."""
        return decompress_doc(view, self._ctx())

    # --- whole-directory convenience ---------------------------------------

    def compress_directory(self, input_dir: str, out_dir: str,
                           verify: bool = False) -> Dict[str, Any]:
        """Compress a corpus directory to `out_dir` (delegates to run_corpus).
        Note: this uses a fresh internal model for the on-disk artifact and
        does not mutate this codec's in-memory model."""
        return run_corpus(input_dir, out_dir, verify=verify)


def compress_document(doc: Any) -> Dict[str, Any]:
    """Compress a single PROV-JSON document into a self-contained package.

    The returned dict ``{"compprograph": <ver>, "model": ..., "view": ...}`` is
    JSON-serialisable and round-trips entirely on its own -- it embeds the
    minimal model (only the strings/templates this document uses), so no
    external state is required to restore it.

    For more than a handful of documents, prefer a shared `CompProGraphCodec`
    so structure is de-duplicated across the whole corpus instead of being
    re-embedded in every package.
    """
    codec = CompProGraphCodec()
    view = codec.compress(doc)
    return {"compprograph": __version__, "model": codec.dump_model(), "view": view}


def decompress_document(package: Dict[str, Any]) -> Any:
    """Inverse of `compress_document`: reconstruct the original document from a
    self-contained package."""
    codec = CompProGraphCodec.from_model(package["model"])
    return codec.decompress(package["view"])


# Friendly directory-level aliases (the engine functions, renamed for the API).
compress_directory = run_corpus
decompress_directory = decompress_corpus
verify_directory = verify_corpus


# =============================================================================
# CLI
# =============================================================================

def cmd_compress(args):
    res = run_corpus(args.input, args.out, verify=args.verify)
    print(json.dumps(res["agg"], indent=2))


def cmd_decompress(args):
    n = decompress_corpus(args.input, args.out)
    print(json.dumps({"reconstructed_files": n, "output_dir": args.out}, indent=2))


def cmd_verify(args):
    res = verify_corpus(args.orig, args.compressed)
    print(json.dumps(res, indent=2))
    if not res["lossless"]:
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser(description="CompProGraph: lossless structural compression for PROV-JSON corpora")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress", help="Compress a corpus (structural stage)")
    c.add_argument("--input", required=True, help="Directory with PROV-JSON files")
    c.add_argument("--out", required=True, help="Output directory")
    c.add_argument("--verify", action="store_true", help="Run lossless round-trip verification")
    c.set_defaults(func=cmd_compress)

    d = sub.add_parser("decompress", help="Reconstruct originals from a compressed corpus")
    d.add_argument("--input", required=True, help="Compressed corpus directory (run_corpus output)")
    d.add_argument("--out", required=True, help="Directory for reconstructed documents")
    d.set_defaults(func=cmd_decompress)

    v = sub.add_parser("verify", help="Verify a compressed corpus reconstructs the originals")
    v.add_argument("--orig", required=True, help="Original corpus directory")
    v.add_argument("--compressed", required=True, help="Compressed corpus directory")
    v.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
