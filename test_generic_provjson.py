#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generic W3C PROV-JSON round-trip tests for the CompProGraph codec.

These exercise the parts of spec-compliant PROV-JSON that the OpenML-CC18 corpus
the codec was originally tuned on does NOT contain, and which the three coverage
extensions added:

  1. The full set of W3C PROV-DM relation types (not just the 7 OpenML uses) is
     columnarised -- e.g. wasStartedBy / specializationOf / alternateOf / hadMember.
  2. Typed-literal field values  {"$": v, "type": "xsd:..."}  (and numbers, bools,
     JSON null, nested objects) are columnar-safe, not just plain strings.
  3. Bundles (named nested documents under the top-level `bundle` key) are
     compressed recursively through the same shared tables.

Every test asserts EXACT (canonical-JSON) round-trip through BOTH public API
paths -- the self-contained `compress_document` and the shared-model
`CompProGraphCodec` -- and several also assert that the intended transform
actually fired (a marker is present) rather than silently falling back to the
generic encoding. Two further tests re-check real corpora so the extensions
cannot have regressed either path: an OpenML-CC18 document (the tuned path) and a
sample of real AgentDojo-PROV LLM-agent documents (the untuned path); both are
skipped gracefully if the corpus is not present. A sidecar-exclusion test guards
that AgentDojo transcripts/manifests are never swept into corpus compression.

Run directly (no test framework needed):   python test_generic_provjson.py
Or under pytest:                            pytest test_generic_provjson.py
"""
import glob
import json
import sys

import compprograph_compress as cpg


# The AgentDojo-PROV per-model corpora live under corpus-prov/ (six LLM models,
# 2,944 PROV graphs each). The real-corpus tests below discover them here and
# skip gracefully if the corpus is not present.
AGENTDOJO_ROOTS = (
    "corpus-prov/prov-deepseek-chat",
    "corpus-prov/prov-gpt-5-nano",
    "corpus-prov/google_gemini-2.5-flash",
    "corpus-prov/meta-llama_llama-3.3-70b-instruct",
    "corpus-prov/qwen_qwen-2.5-72b-instruct",
    "corpus-prov/z-ai_glm-4.6",
)


# ---------------------------------------------------------------------------
# A spec-style W3C PROV-JSON document exercising every tricky feature at once.
# ---------------------------------------------------------------------------

def build_w3c_doc():
    return {
        "prefix": {
            "ex": "http://example.org/",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
        },
        "entity": {
            # typed-literal value + a volatile attr (prov:label) -> residual split
            "ex:report1": {"prov:type": "ex:Report", "prov:label": "Report v1",
                           "prov:value": {"$": "42", "type": "xsd:integer"}},
            "ex:report2": {"prov:type": "ex:Report"},
            "ex:collection1": {"prov:type": "prov:Collection"},
        },
        "activity": {
            "ex:edit1": {
                "prov:startTime": {"$": "2011-11-16T16:05:00", "type": "xsd:dateTime"},
                "prov:endTime":   {"$": "2011-11-16T16:06:00", "type": "xsd:dateTime"},
            },
            "ex:edit2": {},
        },
        "agent": {
            "ex:bob": {"prov:type": {"$": "prov:Person", "type": "prov:QUALIFIED_NAME"}},
        },

        # --- relations the ORIGINAL 7-entry schema covered ---
        "used": {
            # string fields (bare-int columns) + a typed-literal time (list column)
            "_:u1": {"prov:activity": "ex:edit1", "prov:entity": "ex:report1",
                     "prov:time": {"$": "2011-11-16T16:05:30", "type": "xsd:dateTime"}},
            # same relation, some schema fields ABSENT (null columns) -> must stay distinct
            "_:u2": {"prov:activity": "ex:edit2", "prov:entity": "ex:report2"},
            # a schema field explicitly null  -> must reconstruct as null, NOT as absent
            "_:u3": {"prov:activity": "ex:edit1", "prov:entity": "ex:report2",
                     "prov:role": None},
        },
        "wasGeneratedBy": {
            "_:g1": {"prov:entity": "ex:report2", "prov:activity": "ex:edit1",
                     # off-schema extra (preserved verbatim) + a numeric off-schema value
                     "ex:confidence": 0.97, "ex:ordinal": 3, "ex:flag": True},
        },
        "wasAssociatedWith": {
            "_:a1": {"prov:activity": "ex:edit1", "prov:agent": "ex:bob"},
        },

        # --- relations only the EXTENDED schema covers (extension 1) ---
        "wasStartedBy": {"_:s1": {"prov:activity": "ex:edit1", "prov:trigger": "ex:report1",
                                  "prov:time": {"$": "2011-11-16T16:05:00", "type": "xsd:dateTime"}}},
        "wasEndedBy":   {"_:e1": {"prov:activity": "ex:edit1", "prov:trigger": "ex:report2"}},
        "wasInvalidatedBy": {"_:i1": {"prov:entity": "ex:report1", "prov:activity": "ex:edit2"}},
        "wasInfluencedBy":  {"_:wi1": {"prov:influencee": "ex:report2", "prov:influencer": "ex:report1"}},
        "specializationOf": {"_:sp1": {"prov:specificEntity": "ex:report2",
                                       "prov:generalEntity": "ex:report1"}},
        "alternateOf":  {"_:al1": {"prov:alternate1": "ex:report1", "prov:alternate2": "ex:report2"}},
        "hadMember":    {"_:hm1": {"prov:collection": "ex:collection1", "prov:entity": "ex:report1"}},

        # --- a relation type NOT in any schema -> generic fallback (still lossless) ---
        "hadDictionaryMember": {"_:dm1": {"prov:dictionary": "ex:collection1",
                                          "prov:key": "k", "prov:entity": "ex:report1"}},

        # --- a bundle (nested document) (extension 3) ---
        "bundle": {
            "ex:bundle1": {
                "entity": {"ex:e3": {"prov:type": "ex:Thing",
                                     "prov:value": {"$": "3.14", "type": "xsd:double"}}},
                "activity": {"ex:run1": {}},
                "used": {"_:bu1": {"prov:activity": "ex:run1", "prov:entity": "ex:e3",
                                   "prov:time": {"$": "2012-01-01T00:00:00", "type": "xsd:dateTime"}}},
                "specializationOf": {"_:bsp1": {"prov:specificEntity": "ex:e3",
                                                "prov:generalEntity": "ex:report1"}},
                "wasDerivedFrom": {"_:bd1": {"prov:generatedEntity": "ex:e3",
                                             "prov:usedEntity": "ex:report1"}},
            },
            "ex:bundle2": {
                "entity": {"ex:e4": {"prov:type": "ex:Thing"}},
            },
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roundtrip_self_contained(doc):
    """Through compress_document / decompress_document, forced through JSON."""
    pkg = cpg.compress_document(doc)
    pkg = json.loads(json.dumps(pkg, ensure_ascii=False))   # prove it serialises
    return cpg.decompress_document(pkg)


def _roundtrip_shared_model(docs):
    """Through a shared CompProGraphCodec; returns {name: restored_doc}. The model
    and every view are forced through JSON so we test the serialised forms."""
    codec = cpg.CompProGraphCodec()
    views = {name: json.loads(json.dumps(codec.compress(d))) for name, d in docs.items()}
    codec2 = cpg.CompProGraphCodec.from_model_json(codec.dump_model_json())
    return {name: codec2.decompress(v) for name, v in views.items()}


def _section_markers(doc):
    """Map each top-level section -> which transform encoded it, via a fresh codec."""
    codec = cpg.CompProGraphCodec()
    view = codec.compress(doc)
    out = {}
    for k, v in view.items():
        if isinstance(v, dict) and cpg.NODE_MARKER in v and len(v) == 1:
            out[k] = "node"
        elif isinstance(v, dict) and cpg.EDGE_MARKER in v and len(v) == 1:
            out[k] = "edge"
        elif isinstance(v, dict) and cpg.BUNDLE_MARKER in v and len(v) == 1:
            out[k] = "bundle"
        else:
            out[k] = "generic"
    return view, out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_self_contained_roundtrip():
    doc = build_w3c_doc()
    restored = _roundtrip_self_contained(doc)
    assert cpg.canonical(restored) == cpg.canonical(doc), "self-contained round trip changed the doc"


def test_shared_model_roundtrip():
    doc = build_w3c_doc()
    # three identical copies must all reconstruct against the one shared model
    restored = _roundtrip_shared_model({"d1": doc, "d2": doc, "d3": doc})
    for name, r in restored.items():
        assert cpg.canonical(r) == cpg.canonical(doc), f"shared-model round trip changed {name}"


def test_extended_relations_are_columnar():
    """Extension 1: relations beyond the original 7 are columnarised."""
    _view, markers = _section_markers(build_w3c_doc())
    for rel in ("wasStartedBy", "wasEndedBy", "wasInvalidatedBy", "wasInfluencedBy",
                "specializationOf", "alternateOf", "hadMember"):
        assert markers[rel] == "edge", f"{rel} should be columnar, got {markers[rel]}"


def test_typed_literals_are_columnar():
    """Extension 2: a section whose schema field is a typed literal still goes
    columnar (previously this forced the whole section to the generic fallback)."""
    _view, markers = _section_markers(build_w3c_doc())
    assert markers["used"] == "edge", f"`used` (typed prov:time) should be columnar, got {markers['used']}"
    assert markers["wasStartedBy"] == "edge"


def test_null_field_vs_absent_field():
    """A schema field present-but-null must round-trip as null and stay distinct
    from the same field being absent on another edge."""
    doc = build_w3c_doc()
    restored = _roundtrip_self_contained(doc)
    u = restored["used"]
    assert "prov:role" in u["_:u3"] and u["_:u3"]["prov:role"] is None, "present-null field lost"
    assert "prov:role" not in u["_:u1"], "absent field leaked in"
    assert "prov:role" not in u["_:u2"], "absent field leaked in"


def test_offschema_typed_extras_preserved():
    """Off-schema fields (numbers, bools) are preserved verbatim in the extras."""
    doc = build_w3c_doc()
    restored = _roundtrip_self_contained(doc)
    g = restored["wasGeneratedBy"]["_:g1"]
    assert g["ex:confidence"] == 0.97 and g["ex:ordinal"] == 3 and g["ex:flag"] is True


def test_unknown_relation_falls_back_but_roundtrips():
    """A relation type in NO schema uses the generic fallback yet stays lossless."""
    doc = build_w3c_doc()
    _view, markers = _section_markers(doc)
    assert markers["hadDictionaryMember"] == "generic"
    restored = _roundtrip_self_contained(doc)
    assert cpg.canonical(restored["hadDictionaryMember"]) == cpg.canonical(doc["hadDictionaryMember"])


def test_bundles_are_recursed():
    """Extension 3: the bundle section is encoded with the bundle marker, and the
    nested documents are themselves templated/columnarised."""
    view, markers = _section_markers(build_w3c_doc())
    assert markers["bundle"] == "bundle", f"bundle should use BUNDLE_MARKER, got {markers['bundle']}"
    # peek inside: the first bundle member's `used`/`entity` must be encoded, not raw
    bids, bviews = view["bundle"][cpg.BUNDLE_MARKER]
    inner = bviews[0]
    assert cpg.NODE_MARKER in inner["entity"], "nested entity not templated"
    assert cpg.EDGE_MARKER in inner["used"], "nested edge not columnarised"
    assert cpg.EDGE_MARKER in inner["specializationOf"], "nested extended relation not columnarised"


def test_bundle_roundtrip_exact():
    doc = build_w3c_doc()
    restored = _roundtrip_self_contained(doc)
    assert cpg.canonical(restored["bundle"]) == cpg.canonical(doc["bundle"])


def test_empty_and_degenerate_objects():
    """Empty / minimal / oddly-shaped PROV-JSON *documents* (always JSON objects,
    per the spec) must round-trip -- including objects whose only key collides
    with the codec's internal markers/leaf sentinel."""
    docs = [
        {},
        {"prefix": {}},
        {"entity": {}},
        {"bundle": {}},
        {"$": 5},          # top-level key equal to the string-leaf sentinel
        {"$": "x"},
        {"_n": "x"},       # top-level key equal to the node marker
        {"used": [1, 2, 3]},   # a section name whose value isn't an edge dict
    ]
    for doc in docs:
        restored = _roundtrip_self_contained(doc)
        assert cpg.canonical(restored) == cpg.canonical(doc), f"degenerate object changed: {doc!r}"


def test_top_level_string_rejected():
    """A bare top-level JSON string is not a PROV-JSON document and its encoding
    would be ambiguous with the object document {"$": id} -- it must raise
    ValueError rather than silently corrupt."""
    for bad in ("hello", ""):
        try:
            cpg.compress_document(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"top-level string {bad!r} should raise ValueError")
    # Other non-object top-levels are out of spec but unambiguous: still exact.
    for ok in (42, 3.5, True, None, [1, "a", {"k": "v"}], []):
        restored = _roundtrip_self_contained(ok)
        assert cpg.canonical(restored) == cpg.canonical(ok), f"non-object {ok!r} changed"


def test_marker_named_keys_do_not_collide():
    """Original object keys that happen to equal the codec's internal markers
    ($, #5, _n, _e, _b) must survive (they are interned like any other key)."""
    doc = {"entity": {"ex:x": {"$": "dollar", "#5": "hash", "_n": "node",
                               "_e": "edge", "_b": "bundle", "prov:type": "ex:T"}}}
    restored = _roundtrip_self_contained(doc)
    assert cpg.canonical(restored) == cpg.canonical(doc), "marker-named keys collided"


def test_non_prov_sidecars_excluded():
    """AgentDojo-PROV ships non-PROV sidecars (transcripts, manifest) next to the
    graphs; the shared discovery predicate must skip them so the codec and the
    benchmark agree on exactly which files are compressed."""
    assert cpg.is_corpus_prov_file("x/prov/benign/task.json") is True
    assert cpg.is_corpus_prov_file("x/prov/benign/task.transcript.json") is False
    assert cpg.is_corpus_prov_file("x/manifest.json") is False
    assert cpg.is_corpus_prov_file("x/checksums.sha256") is False
    # And discovery on a real AgentDojo tree (if present) yields no sidecars.
    roots = [g for g in AGENTDOJO_ROOTS if glob.glob(g + "/prov/**/*.json", recursive=True)]
    if not roots:
        print("  (sidecar discovery check skipped: no AgentDojo corpus present)")
        return
    found = cpg.find_json_files_recursive(roots[0])
    assert found, "expected to discover AgentDojo PROV graphs"
    assert not any(p.endswith(".transcript.json") or p.endswith("manifest.json") for p in found), \
        "a non-PROV sidecar leaked into corpus discovery"


def test_agentdojo_corpus_doc_not_regressed():
    """Real AgentDojo-PROV (LLM-agent) documents must round-trip exactly through
    both API paths -- the untuned codec is lossless on this corpus."""
    samples = []
    for g in AGENTDOJO_ROOTS:
        per_model = sorted(p for p in glob.glob(g + "/prov/**/*.json", recursive=True)
                           if cpg.is_corpus_prov_file(p))
        samples += per_model[:15]       # a handful per model catches regressions
    if not samples:
        print("  (skipped: no AgentDojo-PROV corpus present)")
        return
    docs = {}
    for p in samples:
        doc = json.load(open(p, encoding="utf-8"))
        assert cpg.canonical(_roundtrip_self_contained(doc)) == cpg.canonical(doc), \
            f"AgentDojo doc changed (self-contained): {p}"
        docs[p] = doc
    restored = _roundtrip_shared_model(docs)   # cross-document shared model
    for p, d in docs.items():
        assert cpg.canonical(restored[p]) == cpg.canonical(d), \
            f"AgentDojo doc changed (shared model): {p}"
    print(f"  (AgentDojo regression: {len(samples)} docs across {len({s.split('/')[1] for s in samples})} model(s))")


def test_openml_corpus_doc_not_regressed():
    """A real OpenML PROV-JSON doc (the tuned path) must still round-trip exactly."""
    candidates = glob.glob("prov_corpus_light/**/*.json", recursive=True)
    sample = next((p for p in candidates
                   if '"entity"' in open(p, encoding="utf-8").read()), None)
    if sample is None:
        print("  (skipped: no OpenML corpus doc with an entity section found)")
        return
    doc = json.load(open(sample, encoding="utf-8"))
    assert cpg.canonical(_roundtrip_self_contained(doc)) == cpg.canonical(doc)
    restored = _roundtrip_shared_model({"d": doc})
    assert cpg.canonical(restored["d"]) == cpg.canonical(doc)
    print(f"  (regression doc: {sample})")


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # pragma: no cover - surfaces unexpected errors
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
