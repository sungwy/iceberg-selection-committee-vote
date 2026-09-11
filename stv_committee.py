#!/usr/bin/env python3
"""Selection committee election: ballot anonymization and constrained STV count.

Counting uses Meek STV from Apache STeVe (monitoring/stv_tool.py), unmodified.
Constraints are applied by exclude-and-recount on the same ballots:
  1. At most one winner per company. Among company-mates elected in the same
     count, the earliest-elected is kept and the rest are excluded
     permanently. A PMC member is kept instead only when excluding them would
     leave fewer than PMC_MIN companies with an eligible PMC candidate.
  2. At least PMC_MIN PMC winners. If short, the count is rerun with non-PMC
     candidates limited to the (SEATS - PMC_MIN) earliest-elected non-PMC
     winners, so the remaining seats go to PMC members.
Company conflicts are resolved first; a conflict clears any PMC-minimum limit
and the next pass starts from the permanent exclusions only.

Tie-breaks for "earliest elected": election round, then Meek vote total at
election, then first-preference votes, then a draw seeded by --seed.

If every ballot is exhausted before all seats fill, the open seats are filled
in seeded draw order from companies not yet represented, PMC members first
until the PMC minimum is met.

Subcommands:
  anonymize  (run by the chair) raw Google Forms CSV -> anonymized ballots CSV
  count      anonymized ballots CSV + candidates file -> result and audit log
"""

import argparse
import collections
import contextlib
import csv
import importlib.util
import io
import pathlib
import random
import re
import secrets
import sys

RANK_COL = re.compile(r'^Rank\s+(\d+)$', re.I)


# ---------------------------------------------------------------- inputs

def rank_columns(header):
    cols = [(int(m.group(1)), i) for i, h in enumerate(header)
            if (m := RANK_COL.match(h.strip()))]
    if not cols:
        sys.exit('ERROR: no "Rank N" columns found')
    return [i for _, i in sorted(cols)]


def load_candidates(path):
    delim = '\t' if path.suffix.lower() in ('.tsv', '.txt') else ','
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f, delimiter=delim))
    cands = {}
    for r in rows:
        name = (r.get('Name') or '').strip()
        company = (r.get('Company') or '').strip()
        pmc = (r.get('PMC') or '').strip().upper()
        if not name:
            continue
        if name in cands:
            sys.exit(f'ERROR: duplicate candidate name: {name!r}')
        if not company:
            sys.exit(f'ERROR: missing company for {name!r}')
        if pmc not in ('Y', 'N'):
            sys.exit(f'ERROR: PMC must be Y or N for {name!r}, got {pmc!r}')
        cands[name] = {'company': company, 'pmc': pmc == 'Y'}
    return cands


def parse_ballots(path, candidates):
    """Published rules: blanks skipped; a repeated name counts only at its
    highest rank; unknown names are an error; empty ballots are dropped.
    Ballot cells may hold the bare name or the form label "Name (Company)"."""
    labels = {f'{n} ({c["company"]})': n for n, c in candidates.items()}
    labels.update({n: n for n in candidates})
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = rank_columns(header)
        ballots, empty = [], 0
        for lineno, row in enumerate(reader, start=2):
            seen, ranking = set(), []
            for i in cols:
                cell = row[i].strip() if i < len(row) else ''
                if not cell:
                    continue
                name = labels.get(cell)
                if name is None:
                    sys.exit(f'ERROR: line {lineno}: unknown candidate {cell!r}')
                if name not in seen:
                    seen.add(name)
                    ranking.append(name)
            if ranking:
                ballots.append(ranking)
            else:
                empty += 1
    return ballots, empty


def load_stv_tool(steve_dir):
    path = pathlib.Path(steve_dir) / 'monitoring' / 'stv_tool.py'
    if not path.is_file():
        sys.exit(f'ERROR: {path} not found (clone https://github.com/apache/steve)')
    spec = importlib.util.spec_from_file_location('stv_tool', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- counting

def run_meek(stv, names, ballots, seats):
    """Run STeVe's Meek STV. Returns ({name: (round, vote)}, exhausted).
    If all ballots are exhausted before every seat fills, returns the
    candidates elected up to that point with exhausted=True."""
    rnd = [0]
    elected = {}
    orig_iter, orig_elect = stv.iterate_one, stv.Candidate.elect

    def iterate_one(*a, **kw):
        rnd[0] += 1
        return orig_iter(*a, **kw)

    def elect(self):
        elected.setdefault(self.name, (rnd[0], self.vote or 0.0))
        return orig_elect(self)

    stv.iterate_one, stv.Candidate.elect = iterate_one, elect
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = stv.run_stv(names, ballots, seats) if ballots else None
    except Exception as e:
        # STeVe aborts when every ballot is exhausted before all seats fill.
        if 'very low quota' not in str(e):
            raise
        result = None
    finally:
        stv.iterate_one, stv.Candidate.elect = orig_iter, orig_elect

    if result is None:
        return dict(elected), True
    final = {c.name for c in result.l if c.status == stv.ELECTED}
    return {n: elected[n] for n in final}, False


def fill_by_draw(winners, remaining, candidates, draw_rank, seats, pmc_min):
    """Fill seats left open by ballot exhaustion, in seeded draw order: PMC
    members first until the PMC minimum is met, then any candidate. Only
    candidates from companies not yet represented are drawn."""
    last_round = max((r for r, _ in winners.values()), default=0) + 1
    pool = sorted((n for n in remaining if n not in winners), key=draw_rank.get)

    def take(want_pmc):
        # A PMC member drawn for the minimum may share a company with a non-PMC
        # winner; the resulting conflict is resolved like any other.
        taken = {candidates[n]['company'] for n in winners
                 if candidates[n]['pmc'] or not want_pmc}
        for n in pool:
            if n not in winners and candidates[n]['company'] not in taken \
                    and (not want_pmc or candidates[n]['pmc']):
                winners[n] = (last_round, 0.0)
                return True
        return False

    while len(winners) < seats and \
            sum(candidates[n]['pmc'] for n in winners) < pmc_min and take(True):
        pass
    while len(winners) < seats and take(False):
        pass
    if len(winners) < seats:
        sys.exit('ERROR: not enough eligible candidates to fill all seats')


def count(args):
    candidates = load_candidates(pathlib.Path(args.candidates))
    ballots, empty = parse_ballots(args.ballots, candidates)
    stv = load_stv_tool(args.steve)

    first_prefs = collections.Counter(b[0] for b in ballots)
    draw = sorted(candidates)
    random.Random(args.seed).shuffle(draw)
    draw_rank = {n: i for i, n in enumerate(draw)}

    def order_key(name, winners):
        rnd, vote = winners[name]
        return (rnd, -vote, -first_prefs[name], draw_rank[name])

    log = []

    def say(line):
        log.append(line)
        print(line, flush=True)
    say(f'Ballots counted: {len(ballots)} (empty ballots dropped: {empty})')
    say(f'Candidates: {len(candidates)}  Seats: {args.seats}  '
        f'PMC minimum: {args.pmc_min}  Seed: {args.seed}')
    say('First preferences: ' + ', '.join(
        f'{n} {c}' for n, c in first_prefs.most_common()))

    def fmt(n):
        c = candidates[n]
        return f'{n}  ({c["company"]}{", PMC" if c["pmc"] else ""})'

    def feasible(excl):
        rest = [n for n in candidates if n not in excl]
        return (len({candidates[n]['company'] for n in rest}) >= args.seats and
                len({candidates[n]['company'] for n in rest
                     if candidates[n]['pmc']}) >= args.pmc_min)

    if not feasible(set()):
        sys.exit('ERROR: constraints cannot be satisfied by the candidate list')

    excluded = []   # permanent, from company conflicts
    doomed = set()  # non-PMC candidates set aside for one PMC-minimum pass
    for pass_no in range(1, 2 * len(candidates) + 2):
        out = set(excluded) | doomed
        remaining = sorted(n for n in candidates if n not in out)
        votes = [[n for n in b if n not in out] for b in ballots]
        votes = [v for v in votes if v]
        winners, exhausted = run_meek(stv, remaining, votes, args.seats)
        if exhausted:
            fill_by_draw(winners, remaining, candidates, draw_rank,
                         args.seats, args.pmc_min)
        ordered = sorted(winners, key=lambda n: order_key(n, winners))

        say(f'\nPass {pass_no}: excluded: {", ".join(excluded) or "none"}')
        if doomed:
            say(f'  PMC-minimum pass: {len(doomed)} non-PMC candidates set aside')
        if exhausted:
            say('  all ballots exhausted; open seats filled by seeded draw')
        for n in ordered:
            rnd, vote = winners[n]
            say(f'  round {rnd:>2}  {vote:8.3f}  {fmt(n)}')

        by_company = collections.defaultdict(list)
        for n in ordered:
            by_company[candidates[n]['company']].append(n)
        conflicts = [ns for ns in by_company.values() if len(ns) > 1]
        if conflicts:
            for ns in conflicts:
                kept = ns[0]
                if not feasible(set(excluded) | set(ns[1:])):
                    kept = next(n for n in ns if candidates[n]['pmc'])
                drop = [n for n in ns if n != kept]
                note = '' if kept == ns[0] else ' (keeps PMC minimum reachable)'
                say(f'  company conflict {candidates[kept]["company"]}: '
                    f'keep {kept}{note}, exclude {", ".join(drop)}')
                excluded.extend(drop)
            doomed = set()
            continue

        pmc_winners = [n for n in ordered if candidates[n]['pmc']]
        if len(pmc_winners) < args.pmc_min:
            non_pmc = [n for n in ordered if not candidates[n]['pmc']]
            kept = non_pmc[:args.seats - args.pmc_min]
            doomed = {n for n in candidates
                      if not candidates[n]['pmc'] and n not in kept
                      and n not in excluded}
            say(f'  PMC winners {len(pmc_winners)} < {args.pmc_min}: rerun with '
                f'non-PMC candidates limited to the {len(kept)} earliest-elected')
            continue

        say('\nFinal committee:')
        for n in ordered:
            say(f'  {fmt(n)}')
        break
    else:
        sys.exit('ERROR: procedure did not converge')

    if args.log:
        pathlib.Path(args.log).write_text('\n'.join(log) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- anonymize

def anonymize(args):
    eligible = {l.strip().lower() for l in open(args.eligible, encoding='utf-8')
                if l.strip()}
    with open(args.responses, newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)
        email_col = next((i for i, h in enumerate(header)
                          if 'email' in h.lower()), None)
        if email_col is None:
            sys.exit('ERROR: no email column in responses')
        cols = rank_columns(header)
        latest = {}
        for row in reader:
            latest[row[email_col].strip().lower()] = row  # last row wins

    accepted = [r for e, r in latest.items() if e in eligible]
    rejected = sorted(e for e in latest if e not in eligible)
    secrets.SystemRandom().shuffle(accepted)

    with open(args.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([header[i] for i in cols])
        for r in accepted:
            w.writerow([r[i] if i < len(r) else '' for i in cols])

    print(f'Accepted ballots: {len(accepted)}', file=sys.stderr)
    for e in rejected:
        print(f'Rejected (not on eligible list): {e}', file=sys.stderr)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    a = sub.add_parser('anonymize', help='strip identities from form responses')
    a.add_argument('responses', help='Google Forms response CSV')
    a.add_argument('--eligible', required=True,
                   help='file with one eligible voter email per line')
    a.add_argument('--out', required=True, help='anonymized ballots CSV')
    a.set_defaults(func=anonymize)

    c = sub.add_parser('count', help='run the constrained STV count')
    c.add_argument('ballots', help='anonymized ballots CSV')
    c.add_argument('candidates', help='candidates TSV/CSV: Name, Company, PMC')
    c.add_argument('--steve', required=True, help='path to apache/steve clone')
    c.add_argument('--seats', type=int, default=11)
    c.add_argument('--pmc-min', type=int, default=3)
    c.add_argument('--seed', required=True,
                   help='tie-break seed, published before voting opens')
    c.add_argument('--log', help='write the audit log to this file')
    c.set_defaults(func=count)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
