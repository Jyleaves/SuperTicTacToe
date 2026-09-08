"""Seeded, color-swapped matches against a Git revision, with tree reuse.

Run from the repository root. Output JSONL records enough information to replay
each game. This measures the Rust search, excluding the GUI and background eval.
"""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def paired_interval(rows):
    """Bootstrap whole color-swapped pairs, preserving within-pair dependence."""
    scores = [1.0 if r['outcome'] == 0 else 0.5 if r['outcome'] == 1 else 0.0
              for r in rows]
    pairs = [(scores[i] + scores[i + 1]) / 2 for i in range(0, len(scores), 2)]
    rng = random.Random(20260908)
    samples = sorted(sum(rng.choices(pairs, k=len(pairs))) / len(pairs)
                     for _ in range(5000))
    return sum(scores) / len(scores), [samples[125], samples[4874]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', default='v1.1.1')
    parser.add_argument('--games', type=int, default=120)
    parser.add_argument('--iterations', type=int, default=8000)
    parser.add_argument('--time-ms', type=float, default=0,
                        help='per-move budget; 0 runs all iterations')
    parser.add_argument('--seed', type=int, default=20260908)
    parser.add_argument('--goal', type=int, choices=[1, -1], default=1)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--portable', action='store_true', help='force both software CPU kernels')
    parser.add_argument('--reference-pool', choices=['full', 'adaptive'], default='full',
                        help='v1.1.0 used adaptive pools; v1.0.0 uses full')
    parser.add_argument('--candidate-pool', choices=['full', 'adaptive'], default='full')
    parser.add_argument('--cpu', type=int)
    parser.add_argument('--equivalence', action='store_true',
                        help='compare both searches at every ply on the same trajectory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.games < 2 or args.games % 2:
        parser.error('--games must be positive and even')
    if not 1 <= args.iterations <= 2_000_000:
        parser.error('--iterations must be in 1..2000000')
    if not 1 <= args.threads <= 32:
        parser.error('--threads must be in 1..32')
    if not math.isfinite(args.time_ms) or args.time_ms < 0:
        parser.error('--time-ms must be finite and nonnegative')
    if not 0 <= args.seed < 2**64:
        parser.error('--seed must be a u64')
    if args.equivalence and args.time_ms:
        parser.error('deterministic equivalence requires an iteration budget')
    if args.goal == -1 and not args.equivalence:
        parser.error("--goal -1 is supported in --equivalence mode")
    revision = subprocess.check_output(
        ['git', 'rev-parse', '--verify', '--end-of-options', args.reference + '^{commit}'],
        cwd=ROOT, text=True).strip()
    reference = subprocess.check_output(
        ['git', 'show', revision + ':rust/src/mcts.rs'], cwd=ROOT)
    candidate = (ROOT / 'rust/src/mcts.rs').read_bytes()
    if args.portable and b'feature = "portable"' not in reference:
        parser.error('reference does not implement the portable feature')
    capacity = lambda kind: 524288 if kind == 'full' else min(524288, 65536 + 2 * args.iterations)
    config = dict(type='configuration', reference=revision,
                  candidate_sha256=hashlib.sha256(candidate).hexdigest(),
                  games=args.games, iterations=args.iterations, time_ms=args.time_ms,
                  seed=args.seed, cpu=args.cpu, reference_pool=args.reference_pool,
                  candidate_pool=args.candidate_pool, goal=args.goal, portable=args.portable,
                  threads=args.threads, equivalence=args.equivalence,
                  rustc=subprocess.check_output(['rustc', '--version'], text=True).strip())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Generated absolute include paths stay in the temporary directory only.
    with tempfile.TemporaryDirectory(prefix='sttt-strength-') as temporary:
        work = Path(temporary)
        (work / 'reference.rs').write_bytes(reference)
        (work / 'candidate.rs').write_bytes(candidate)
        (work / 'engine.rs').write_bytes((ROOT / 'rust/src/engine.rs').read_bytes())
        (work / 'match.rs').write_bytes((ROOT / 'tests/strength_match.rs').read_bytes())
        (work / 'main.rs').write_text(
            '#![allow(dead_code)]\nmod engine;\nmod reference;\nmod candidate;\n'
            'include!("match.rs");\n', encoding='utf-8')
        executable = work / ('match.exe' if os.name == 'nt' else 'match')
        command = ['rustc', '--edition', '2021', '-O', '-C', 'panic=abort']
        if args.portable:
            command += ['--cfg', 'feature="portable"']
        if os.name == 'nt':
            command += ['-C', 'target-feature=+crt-static']
        subprocess.run(command + ['main.rs', '-o', str(executable)], cwd=work, check=True)
        if args.cpu is not None:
            if args.cpu < 0:
                parser.error('--cpu must be nonnegative')
            if os.name == 'nt':
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
                if not kernel.SetProcessAffinityMask(ctypes.c_void_p(-1), 1 << args.cpu):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                os.sched_setaffinity(0, {args.cpu})
        mode = 'equiv' if args.equivalence else 'duel'
        command = [str(executable), mode, str(args.games), str(args.iterations),
                   str(args.time_ms), str(args.seed), str(capacity(args.candidate_pool)),
                   str(capacity(args.reference_pool)), str(args.goal), str(args.threads)]
        rows = []
        with args.output.open('w', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(config) + '\n')
            print(json.dumps(config), flush=True)
            with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
                for line in process.stdout:
                    row = json.loads(line)
                    rows.append(row)
                    output.write(line)
                    output.flush()
                    if len(rows) % 20 == 0:
                        print(json.dumps(dict(games=len(rows), wdl=row['wdl'])), flush=True)
                if process.wait() != 0:
                    raise RuntimeError('match runner failed')
            if len(rows) != args.games:
                raise RuntimeError('incomplete match set')
            score, interval = (None, None) if args.equivalence else paired_interval(rows)
            summary = dict(type='summary', wdl=rows[-1]['wdl'], score=score,
                           paired_bootstrap_95=interval, positions=rows[-1]['positions'],
                           differences=rows[-1]['differences'],
                           search_seconds=[sum(r['seconds'][i] for r in rows) for i in range(2)])
            output.write(json.dumps(summary) + '\n')
            print(json.dumps(summary), flush=True)
            if args.equivalence and summary['differences']:
                raise SystemExit('deterministic searches differ; see the output records')


if __name__ == '__main__':
    main()
