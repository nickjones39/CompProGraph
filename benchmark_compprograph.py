#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CompProGraph Benchmarking Suite for IEEE Publication.

Reports, per corpus, the LOSSLESS compression achieved by CompProGraph Stage 1
(structural compression) as a PERCENTAGE REDUCTION against the original
PROV-JSON corpus:

    reduction% = (1 - compressed_bytes / original_bytes) * 100

Two figures are reported:

  - Minified JSON : the original documents re-serialised without whitespace.
                    This is the trivial lossless baseline -- it is shown only so
                    the reader can see how much of the headline reduction is
                    genuine structural de-duplication versus free whitespace
                    removal. It is NOT the method.
  - Structural    : CompProGraph Stage 1 -- a corpus-wide string dictionary +
                    supernode templates (Algorithm 1) + columnar edge encoding.
                    This is the reported method.

The structural reduction is also reported relative to the minified baseline
(`struct_vs_minified_percent`) so the structural contribution is unambiguous.

No general-purpose entropy coder (gzip/zstd/xz/brotli) is included in the
reported pipeline: the contribution we measure is the *structural* redundancy
removed by Stage 1, which is semantically meaningful and keeps the output a
queryable PROV-JSON-shaped artifact (unlike an opaque compressed blob). The
structural stage is COMPLEMENTARY to entropy coding, not a competitor: applying
a standard coder to the structural output reduces it further and is the
recommended at-rest storage form. On the light corpus the structural payload
compresses to ~97% total reduction under xz/brotli and ~96.7% under zstd,
versus ~95.5% for gzip -- so gzip is the most portable but the weakest choice,
and zstd is the recommended balance of ratio, speed, and fast decode. This
headroom is discussed as a limitation rather than claimed as part of the
structural result.

Losslessness is verified two ways: (1) an in-process canonical-JSON round-trip
during compression, and (2) an independent full decompression of the persisted
artifact followed by a canonical comparison against the originals. The
reconstruction never sees the original documents.

Usage:
  python benchmark_compprograph.py --corpus-dir /path/to/corpora --output results/
  python benchmark_compprograph.py --corpus prov_corpus_light --output results/

Expected corpus structure:
  corpus_dir/
    prov_corpus_light/  prov_corpus_scaled/  prov_corpus_large/  prov_corpus_full/
"""

import json
import os
import sys
import time
import tracemalloc
import argparse
import shutil
import gc
import tempfile
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Any
from pathlib import Path

import compprograph_compress as compress_mod


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class BenchmarkResult:
    """Single benchmark run result. Sizes are bytes; reductions are percent."""
    corpus_mode: str
    num_files: int
    input_bytes: int

    # Reference baseline: minify only (trivial lossless, NOT the method)
    minified_bytes: int
    minified_reduction_percent: float

    # The method: CompProGraph Stage 1 (structural, lossless)
    struct_bytes: int
    struct_reduction_percent: float          # vs. original on-disk corpus
    struct_vs_minified_percent: float         # vs. minified (the structural-only gain)
    struct_compress_time_s: float
    struct_decompress_time_s: float
    struct_peak_memory_mb: float

    # Losslessness (canonical-JSON round-trip from the artifact)
    struct_lossless: bool = False
    struct_roundtrip_failures: int = 0

    # Structural model stats
    total_supernodes: int = 0
    dict_strings: int = 0
    orig_nodes: int = 0
    orig_edges: int = 0

    # True for a synthetic row aggregating several independently-compressed
    # corpora (e.g. the AgentDojo-PROV per-model corpora pooled into one total).
    is_pooled: bool = False


# Display labels for corpus modes. Known LLM model folders get a typographically
# clean name; OpenML modes keep their capitalised short name; anything else
# (e.g. a pooled-total label) is shown verbatim.
_MODE_LABELS = {
    'prov-deepseek-chat': 'DeepSeek-Chat',
    'prov-gpt-5-nano': 'GPT-5-Nano',
    'google_gemini-2.5-flash': 'Gemini-2.5-Flash',
    'meta-llama_llama-3.3-70b-instruct': 'Llama-3.3-70B',
    'qwen_qwen-2.5-72b-instruct': 'Qwen-2.5-72B',
    'z-ai_glm-4.6': 'GLM-4.6',
}


def clean_mode_label(mode: str) -> str:
    if mode in _MODE_LABELS:
        return _MODE_LABELS[mode]
    if mode.startswith('prov_corpus_'):
        return mode[len('prov_corpus_'):].capitalize()
    return mode


def make_pooled_result(results: List["BenchmarkResult"], name: str) -> "BenchmarkResult":
    """Aggregate several independently-compressed corpora into one total row.

    Each corpus keeps its own shared model (no cross-corpus de-duplication); the
    pooled row simply reports the combined storage footprint and reduction, which
    is the honest 'whole-corpus' number. Byte totals sum; the reduction is
    recomputed from the summed bytes; times sum and peak memory is the max."""
    si = sum(r.input_bytes for r in results)
    sm = sum(r.minified_bytes for r in results)
    ss = sum(r.struct_bytes for r in results)
    return BenchmarkResult(
        corpus_mode=name,
        num_files=sum(r.num_files for r in results),
        input_bytes=si,
        minified_bytes=sm,
        minified_reduction_percent=reduction_pct(sm, si),
        struct_bytes=ss,
        struct_reduction_percent=reduction_pct(ss, si),
        struct_vs_minified_percent=reduction_pct(ss, sm),
        struct_compress_time_s=sum(r.struct_compress_time_s for r in results),
        struct_decompress_time_s=sum(r.struct_decompress_time_s for r in results),
        struct_peak_memory_mb=max((r.struct_peak_memory_mb for r in results), default=0.0),
        struct_lossless=all(r.struct_lossless for r in results),
        struct_roundtrip_failures=sum(r.struct_roundtrip_failures for r in results),
        total_supernodes=sum(r.total_supernodes for r in results),
        dict_strings=sum(r.dict_strings for r in results),
        orig_nodes=sum(r.orig_nodes for r in results),
        orig_edges=sum(r.orig_edges for r in results),
        is_pooled=True,
    )


# =============================================================================
# Size / IO helpers
# =============================================================================

def get_dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except (FileNotFoundError, OSError):
                pass
    return total


def list_json_files(path: str) -> List[str]:
    out = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            if not (f.endswith('.json') or f.endswith('.prov.json')):
                continue
            full = os.path.join(root, f)
            # Skip non-PROV sidecars (AgentDojo transcripts/manifests) so the
            # byte counts here match the set run_corpus actually compresses.
            if not compress_mod.is_corpus_prov_file(full):
                continue
            out.append(full)
    return sorted(out)


def count_json_files(path: str) -> int:
    return len(list_json_files(path))


def structural_payload_size(struct_out: str) -> int:
    """Bytes of the actual compressed payload that must be stored to reconstruct:
    the global tables (compressed_corpus.json) + the per-graph views. The verify
    log and benchmarks.csv report are NOT counted."""
    total = 0
    corpus = os.path.join(struct_out, "compressed_corpus.json")
    if os.path.exists(corpus):
        total += os.path.getsize(corpus)
    total += get_dir_size(os.path.join(struct_out, "compressed_graphs"))
    return total


def canonical_dir_match(orig_dir: str, recon_dir: str) -> Tuple[int, int]:
    """Canonical-JSON compare every original against its reconstruction.
    Returns (checked, failures). A file with no reconstruction counts as a
    failure."""
    checked = 0
    failures = 0
    for fp in list_json_files(orig_dir):
        rel = os.path.relpath(fp, orig_dir)
        rp = os.path.join(recon_dir, rel)
        if not os.path.exists(rp):
            failures += 1
            continue
        checked += 1
        try:
            with open(fp, encoding='utf-8') as f:
                a = compress_mod.canonical(json.load(f))
            with open(rp, encoding='utf-8') as f:
                b = compress_mod.canonical(json.load(f))
        except Exception:
            failures += 1
            continue
        if a != b:
            failures += 1
    return checked, failures


def reduction_pct(compressed: int, original: int) -> float:
    return (1.0 - compressed / original) * 100.0 if original > 0 else 0.0


# =============================================================================
# Per-method measurement
# =============================================================================

def measure_minified(files: List[str]) -> int:
    """Total bytes of each document re-serialised without whitespace."""
    total = 0
    for fp in files:
        with open(fp, encoding='utf-8') as f:
            obj = json.load(f)
        total += len(json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
    return total


def measure_structural(input_dir: str, struct_out: str) -> Tuple[float, float, Dict[str, Any]]:
    """Run Stage 1 with verification. Returns (compress_time_s, peak_mem_mb, agg)."""
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    result = compress_mod.run_corpus(input_dir, struct_out, verify=True)
    elapsed = time.perf_counter() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return elapsed, peak / (1024 * 1024), result.get("agg", {})


# =============================================================================
# Full benchmark for one corpus
# =============================================================================

def run_full_benchmark(corpus_path: str, work_dir: str, mode_name: str) -> BenchmarkResult:
    print(f"\n{'='*64}")
    print(f"Benchmarking: {mode_name}")
    print(f"Input: {corpus_path}")
    print(f"{'='*64}")

    struct_out = os.path.join(work_dir, f"{mode_name}_struct")
    if os.path.exists(struct_out):
        shutil.rmtree(struct_out)

    files = list_json_files(corpus_path)
    input_bytes = sum(os.path.getsize(f) for f in files)
    num_files = len(files)
    print(f"Input: {num_files} files, {input_bytes:,} bytes ({input_bytes/1e6:.2f} MB)")

    # --- Reference baseline: minified JSON ---
    print("\n[Minify] Re-serialising without whitespace (reference baseline)...")
    minified_bytes = measure_minified(files)
    print(f"  {minified_bytes:,} bytes  ({reduction_pct(minified_bytes, input_bytes):.1f}% reduction)")

    # --- The method: Structural (verified lossless) ---
    print("\n[Structural] CompProGraph Stage 1 (dict + supernodes + columnar edges)...")
    struct_time, struct_mem, struct_agg = measure_structural(corpus_path, struct_out)
    struct_bytes = structural_payload_size(struct_out)
    struct_lossless = bool(struct_agg.get("lossless"))
    struct_failures = int(struct_agg.get("roundtrip_failures") or 0)
    print(f"  {struct_bytes:,} bytes  ({reduction_pct(struct_bytes, input_bytes):.1f}% reduction, "
          f"{struct_time:.3f}s, {struct_mem:.1f} MB peak)")
    print(f"  Lossless (round-trip from artifact): {struct_lossless} ({struct_failures} failures)")

    # Independent decompression timing + a second lossless check vs originals.
    print("\n[Structural] Decompressing and checking against originals...")
    recon_out = os.path.join(work_dir, f"{mode_name}_recon")
    t0 = time.perf_counter()
    compress_mod.decompress_corpus(struct_out, recon_out)
    struct_decompress_time = time.perf_counter() - t0
    checked, dec_failures = canonical_dir_match(corpus_path, recon_out)
    struct_lossless = struct_lossless and dec_failures == 0
    print(f"  {struct_decompress_time:.3f}s, {checked} checked, {dec_failures} failures")
    shutil.rmtree(recon_out, ignore_errors=True)

    # --- Summary ---
    struct_red = reduction_pct(struct_bytes, input_bytes)
    struct_vs_min = reduction_pct(struct_bytes, minified_bytes)
    print(f"\n{'-'*64}")
    print(f"SUMMARY: {mode_name}   (reduction vs. original {input_bytes/1e6:.2f} MB)")
    print(f"{'-'*64}")
    print(f"  Minified JSON   {minified_bytes/1e6:8.3f} MB   {reduction_pct(minified_bytes, input_bytes):5.1f}%  (reference only)")
    print(f"  Structural      {struct_bytes/1e6:8.3f} MB   {struct_red:5.1f}%  ({struct_vs_min:.1f}% vs. minified)")
    print(f"  Lossless: {struct_lossless}")
    if not struct_lossless:
        print("  [WARNING] The structural pipeline is NOT lossless on this corpus!")

    return BenchmarkResult(
        corpus_mode=mode_name,
        num_files=num_files,
        input_bytes=input_bytes,
        minified_bytes=minified_bytes,
        minified_reduction_percent=reduction_pct(minified_bytes, input_bytes),
        struct_bytes=struct_bytes,
        struct_reduction_percent=struct_red,
        struct_vs_minified_percent=struct_vs_min,
        struct_compress_time_s=struct_time,
        struct_decompress_time_s=struct_decompress_time,
        struct_peak_memory_mb=struct_mem,
        struct_lossless=struct_lossless,
        struct_roundtrip_failures=struct_failures,
        total_supernodes=int(struct_agg.get("total_supernodes") or 0),
        dict_strings=int(struct_agg.get("dict_strings") or 0),
        orig_nodes=int(struct_agg.get("orig_nodes") or 0),
        orig_edges=int(struct_agg.get("orig_edges") or 0),
    )


# =============================================================================
# Figures (IEEE style)
# =============================================================================

def generate_ieee_figures(results: List[BenchmarkResult], output_dir: str):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    plt.rcParams.update({
        'font.family': 'serif',
        'font.serif': ['Times New Roman', 'Times', 'DejaVu Serif'],
        'font.size': 10, 'axes.labelsize': 10, 'axes.titlesize': 10,
        'xtick.labelsize': 9, 'ytick.labelsize': 9, 'legend.fontsize': 8,
        'figure.dpi': 300, 'savefig.dpi': 300, 'savefig.bbox': 'tight',
    })

    results = sorted(results, key=lambda x: x.input_bytes)
    modes = [clean_mode_label(r.corpus_mode) for r in results]
    input_mb = [r.input_bytes / 1e6 for r in results]
    fig_w = 3.5

    # Figure 1: percentage reduction (the headline figure).
    fig1, ax1 = plt.subplots(figsize=(fig_w, 2.9))
    x = np.arange(len(modes))
    w = 0.35
    ax1.bar(x - w/2, [r.minified_reduction_percent for r in results], w,
            label='Minified (reference)', color='#bab0ac', edgecolor='black', linewidth=0.4)
    bars = ax1.bar(x + w/2, [r.struct_reduction_percent for r in results], w,
                   label='Structural (lossless)', color='#59a14f', edgecolor='black', linewidth=0.4)
    for bar in bars:
        h = bar.get_height()
        ax1.annotate(f'{h:.1f}%', xy=(bar.get_x() + bar.get_width()/2, h),
                     xytext=(0, 2), textcoords="offset points",
                     ha='center', va='bottom', fontsize=7)
    ax1.set_xlabel('Corpus Mode')
    ax1.set_ylabel('Reduction vs. Original (%)')
    ax1.set_title('Lossless Structural Compression')
    ax1.set_xticks(x)
    ax1.set_xticklabels(modes)
    ax1.set_ylim(0, 100)
    ax1.legend(loc='lower right')
    ax1.grid(True, axis='y', linestyle=':', alpha=0.5)
    fig1.tight_layout()
    fig1.savefig(os.path.join(output_dir, 'fig_reduction.pdf'))
    fig1.savefig(os.path.join(output_dir, 'fig_reduction.png'))
    plt.close(fig1)

    # Figure 2: storage footprint (MB).
    fig2, ax2 = plt.subplots(figsize=(fig_w, 2.9))
    ax2.bar(x - w/2, [r.input_bytes / 1e6 for r in results], w,
            label='Original', color='#bab0ac', edgecolor='black', linewidth=0.4)
    ax2.bar(x + w/2, [r.struct_bytes / 1e6 for r in results], w,
            label='Structural', color='#59a14f', edgecolor='black', linewidth=0.4)
    ax2.set_xlabel('Corpus Mode')
    ax2.set_ylabel('Size (MB)')
    ax2.set_title('Storage Footprint')
    ax2.set_xticks(x)
    ax2.set_xticklabels(modes)
    ax2.legend(loc='upper left')
    ax2.grid(True, axis='y', linestyle=':', alpha=0.5)
    fig2.tight_layout()
    fig2.savefig(os.path.join(output_dir, 'fig_storage.pdf'))
    fig2.savefig(os.path.join(output_dir, 'fig_storage.png'))
    plt.close(fig2)

    # Figure 3: scaling (compress time + peak memory vs input size). Pooled
    # aggregate rows are not a real corpus size, so they are excluded from the
    # scaling series; with fewer than three genuine sizes there is nothing to
    # scale, so the figure is skipped entirely.
    scaling = [r for r in results if not r.is_pooled]
    if len({r.input_bytes for r in scaling}) < 3:
        print("\nSkipping scaling figure (need >=3 distinct corpus sizes).")
        print(f"\nGenerated figures in: {output_dir}")
        return
    input_mb = [r.input_bytes / 1e6 for r in scaling]
    fig3, axes = plt.subplots(1, 2, figsize=(7.16, 2.4))
    comp_t = [r.struct_compress_time_s for r in scaling]
    mem = [r.struct_peak_memory_mb for r in scaling]
    for ax, ys, title, ylab, color in [
        (axes[0], comp_t, '(a) Compression Time', 'Time (s)', '#59a14f'),
        (axes[1], mem, '(b) Peak Memory', 'Memory (MB)', '#e15759'),
    ]:
        ax.scatter(input_mb, ys, c=color, s=40, zorder=5)
        if len(input_mb) > 1:
            coeffs = np.polyfit(input_mb, ys, 1)
            tx = np.linspace(0, max(input_mb) * 1.1, 100)
            ax.plot(tx, np.polyval(coeffs, tx), 'k--', linewidth=1.0, alpha=0.7)
            ss_res = sum((y - np.polyval(coeffs, xx))**2 for xx, y in zip(input_mb, ys))
            ss_tot = sum((y - np.mean(ys))**2 for y in ys)
            r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            ax.annotate(f'R^2={r2:.3f}', xy=(0.95, 0.05), xycoords='axes fraction',
                        ha='right', va='bottom', fontsize=8)
        ax.set_xlabel('Input Size (MB)')
        ax.set_ylabel(ylab)
        ax.set_title(title)
        ax.grid(True, linestyle=':', alpha=0.5)
        ax.set_xlim(0, max(input_mb) * 1.15 if input_mb else 1)
        ax.set_ylim(0, max(ys) * 1.15 if ys and max(ys) > 0 else 1)
    fig3.tight_layout()
    fig3.savefig(os.path.join(output_dir, 'fig_scaling.pdf'))
    fig3.savefig(os.path.join(output_dir, 'fig_scaling.png'))
    plt.close(fig3)

    print(f"\nGenerated figures in: {output_dir}")


# =============================================================================
# LaTeX tables
# =============================================================================

def generate_latex_table(results: List[BenchmarkResult], output_dir: str):
    results = sorted(results, key=lambda x: x.input_bytes)

    # Main: percentage reduction + losslessness.
    latex = r"""\begin{table}[htbp]
\centering
\caption{CompProGraph Lossless Structural Compression (\% reduction)}
\label{tab:compression}
\begin{tabular}{lrrrrc}
\toprule
\textbf{Mode} & \textbf{Input} & \textbf{Struct} & \textbf{Reduction} & \textbf{vs.\ Minified} & \textbf{Lossless} \\
              & (MB)           & (MB)            & (\%)               & (\%)                   &                   \\
\midrule
"""
    for r in results:
        mode = clean_mode_label(r.corpus_mode)
        ll = "yes" if r.struct_lossless else "NO"
        latex += (f"{mode} & {r.input_bytes/1e6:.2f} & {r.struct_bytes/1e6:.2f} & "
                  f"{r.struct_reduction_percent:.1f} & {r.struct_vs_minified_percent:.1f} & {ll} \\\\\n")
    latex += "\\bottomrule\n\\end{tabular}\n\\end{table}\n"
    with open(os.path.join(output_dir, 'table_compression.tex'), 'w') as f:
        f.write(latex)

    # Runtime/memory + model stats.
    latex_perf = r"""\begin{table}[htbp]
\centering
\caption{CompProGraph Runtime, Memory, and Model Size}
\label{tab:performance}
\begin{tabular}{lrrrrrr}
\toprule
\textbf{Mode} & \textbf{Files} & \textbf{Compress} & \textbf{Decompress} & \textbf{Peak Mem} & \textbf{Supernodes} & \textbf{Dict} \\
              &                & (s)               & (s)                 & (MB)              &                     & (strings)     \\
\midrule
"""
    for r in results:
        mode = clean_mode_label(r.corpus_mode)
        latex_perf += (f"{mode} & {r.num_files:,} & {r.struct_compress_time_s:.3f} & "
                       f"{r.struct_decompress_time_s:.3f} & {r.struct_peak_memory_mb:.1f} & "
                       f"{r.total_supernodes:,} & {r.dict_strings:,} \\\\\n")
    latex_perf += "\\bottomrule\n\\end{tabular}\n\\end{table}\n"
    with open(os.path.join(output_dir, 'table_performance.tex'), 'w') as f:
        f.write(latex_perf)

    print(f"Generated LaTeX tables in: {output_dir}")


def save_results_json(results: List[BenchmarkResult], output_dir: str):
    data = {
        'benchmark_date': time.strftime('%Y-%m-%d %H:%M:%S'),
        'reduction_definition': '(1 - compressed_bytes / original_bytes) * 100',
        'results': [asdict(r) for r in results],
    }
    with open(os.path.join(output_dir, 'benchmark_results.json'), 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved raw results to: {os.path.join(output_dir, 'benchmark_results.json')}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='CompProGraph Benchmarking Suite for IEEE Publication',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python benchmark_compprograph.py --corpus-dir /path/to/corpora --output results/
  python benchmark_compprograph.py --corpus prov_corpus_light --output results/
        """,
    )
    parser.add_argument('--corpus-dir', type=str,
                        help='Directory containing prov_corpus_* subdirectories')
    parser.add_argument('--corpus', type=str, action='append',
                        help='Specific corpus directory (can be repeated)')
    parser.add_argument('--output', type=str, default='benchmark_results',
                        help='Output directory for results, tables, and figures')
    parser.add_argument('--work-dir', type=str, default=None,
                        help='Working directory for intermediate files (default: a temp dir)')
    parser.add_argument('--skip-figures', action='store_true',
                        help='Skip figure generation')
    parser.add_argument('--pooled-name', type=str, default=None,
                        help='Append a pooled-total row aggregating all benchmarked '
                             'corpora under this label (e.g. "AgentDojo-PROV (pooled)")')
    args = parser.parse_args()

    corpus_paths: List[Tuple[str, str]] = []
    if args.corpus_dir:
        base = Path(args.corpus_dir)
        for mode in ['light', 'scaled', 'large', 'full']:
            p = base / f'prov_corpus_{mode}'
            if p.exists():
                corpus_paths.append((str(p), f'prov_corpus_{mode}'))
    if args.corpus:
        for cp in args.corpus:
            if os.path.exists(cp):
                corpus_paths.append((cp, os.path.basename(cp.rstrip('/'))))

    if not corpus_paths:
        print("Error: No corpus directories found.")
        print("Use --corpus-dir <dir with prov_corpus_* subdirs> or --corpus <dir>.")
        sys.exit(1)

    print(f"Found {len(corpus_paths)} corpus directories to benchmark:")
    for path, name in corpus_paths:
        print(f"  - {name}: {path}")

    os.makedirs(args.output, exist_ok=True)
    work_dir = args.work_dir or tempfile.mkdtemp(prefix="compprograph_bench_")
    os.makedirs(work_dir, exist_ok=True)

    results: List[BenchmarkResult] = []
    for corpus_path, mode_name in corpus_paths:
        try:
            results.append(run_full_benchmark(corpus_path, work_dir, mode_name))
        except Exception as e:
            print(f"Error benchmarking {mode_name}: {e}")
            import traceback
            traceback.print_exc()

    if not results:
        print("No successful benchmark results.")
        sys.exit(1)

    if args.pooled_name and len(results) > 1:
        results.append(make_pooled_result(results, args.pooled_name))

    save_results_json(results, args.output)
    generate_latex_table(results, args.output)
    if not args.skip_figures:
        try:
            generate_ieee_figures(results, args.output)
        except ImportError as e:
            print(f"Warning: could not generate figures (missing matplotlib): {e}")

    # Final summary (percentage is the headline).
    print("\n" + "=" * 80)
    print("BENCHMARK SUMMARY  (% reduction vs. original PROV-JSON)")
    print("=" * 80)
    print(f"{'Mode':<20} {'Input(MB)':>10} {'Struct(MB)':>11} {'Reduction%':>11} "
          f"{'vsMinify%':>10} {'Lossless':>9}")
    print("-" * 80)
    for r in sorted(results, key=lambda x: x.input_bytes):
        mode = clean_mode_label(r.corpus_mode)
        ll = "yes" if r.struct_lossless else "NO"
        print(f"{mode:<20} {r.input_bytes/1e6:>10.2f} {r.struct_bytes/1e6:>11.3f} "
              f"{r.struct_reduction_percent:>11.1f} {r.struct_vs_minified_percent:>10.1f} {ll:>9}")
    print("=" * 80)
    all_lossless = all(r.struct_lossless for r in results)
    print(f"All pipelines lossless across all corpora: {all_lossless}")
    print("\nNote: figures above are the STRUCTURAL stage only (no entropy coder).")
    print("      A general-purpose coder (zstd/xz/brotli; gzip is the weakest)")
    print("      applied to the structural output pushes total lossless reduction")
    print("      higher (~96-97% on light) -- see the limitations discussion.")
    print(f"\nResults saved to: {args.output}/")


if __name__ == '__main__':
    main()
