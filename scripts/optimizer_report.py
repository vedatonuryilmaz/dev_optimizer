#!/usr/bin/env python3
"""Render a Driveline experiment ledger as report.html and report.md.

Read-only over the workspace: it projects autoresearch.jsonl and, optionally, the
reviewed design's ranked hypotheses. It never runs a benchmark or changes state.
"""
import argparse
import html
import importlib.util
import json
import math
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'vendor/driveline-autoresearch/skills/autoresearch/scripts/ar_state.py'
LIMIT = ("Each keep is the worker's own measurement under Driveline's noise-floor rule. "
         "Nothing in this report was independently re-measured.")
TONES = {'keep': 'tone-pass', 'baseline': 'tone-neutral', 'discard': 'tone-neutral',
         'exhausted': 'tone-neutral', 'crash': 'tone-fail', 'checks_failed': 'tone-fail'}


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def md(value):
    value = ('' if value is None else str(value)).replace('\n', ' ').replace('\r', ' ')
    for char in '\\|[]*_`#!':
        value = value.replace(char, '\\' + char)
    return html.escape(value, quote=False)


def num(value, digits=4):
    return format(value, f'.{digits}g') if isinstance(value, (int, float)) and math.isfinite(value) else '–'


def load(workspace):
    """Return validated state plus config headers and result rows from the ledger."""
    spec = importlib.util.spec_from_file_location('_optimizer_report_ar_state', STATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = (workspace / 'autoresearch.jsonl').read_text(encoding='utf-8')
    state = module.parse_state(text)
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    return (state, [r for r in records if r.get('type') == 'config'],
            [r for r in records if r.get('type') != 'config'])


def design_hypotheses(path):
    """Rows of the design's 'Ranked hypotheses' table: rank, id, mechanism."""
    section = re.search(r'^#{2,3}\s+Ranked hypotheses\s*$(.*?)(?=^#{1,3}\s|\Z)', path.read_text(encoding='utf-8'),
                        re.MULTILINE | re.DOTALL | re.IGNORECASE)
    rows = []
    for line in (section.group(1) if section else '').splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if line.strip().startswith('|') and len(cells) >= 3 and not set(cells[0]) <= set('-: ') \
                and cells[0].lower() != 'rank':
            rows.append({'rank': cells[0], 'id': cells[1].strip('`'), 'mechanism': cells[2]})
    return rows


def annotate(configs, results, hypotheses):
    """Add kind, best-so-far before/after and the reviewed hypothesis to each row."""
    ids = {h['id'] for h in hypotheses if h['id']}
    best, segment = None, None
    for row in results:
        if row['segment'] != segment:
            segment, best, first = row['segment'], None, True
        lower = configs[segment]['bestDirection'] == 'lower'
        row['before'] = best
        exhausted = not first and row.get('description', '').startswith('EXHAUSTED:')
        row['kind'] = 'baseline' if first else 'exhausted' if exhausted else row['status']
        if row['status'] == 'keep' and (best is None or (row['metric'] < best if lower else row['metric'] > best)):
            best = row['metric']
        row['best'] = best
        m = re.match(r'\s*[`(\[]*([\w-]+(?:\.[\w-]+)*)', row.get('description', ''))
        row['hypothesis'] = m.group(1) if not exhausted and m and m.group(1) in ids else None
        first = False
    return results


def trajectory(rows, unit):
    """Metric per run with the best-so-far step line; failed runs have no measured value."""
    points = [r for r in rows if r['status'] not in ('crash', 'checks_failed') and r['kind'] != 'exhausted']
    if not points:
        return ''
    w, h, left, right, top, bottom = 960, 264, 54.0, 22.0, 26.0, 34.0
    plot_w, plot_h = w - left - right, h - top - bottom
    values = [r['metric'] for r in points] + [r['best'] for r in points if r['best'] is not None]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.18, abs(low) * 0.1, 1e-2)
    low, high = (max(0.0, low - pad) if min(values) >= 0 else low - pad), high + pad
    count = len(points)

    def sx(index):
        return left + plot_w / 2 if count == 1 else left + index / (count - 1) * plot_w

    def sy(value):
        return top + (1 - (value - low) / (high - low)) * plot_h

    parts = []
    for tick in range(4):
        y = top + tick / 3 * plot_h
        parts.append(f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{w - right:.1f}" y2="{y:.1f}" class="svg-grid"/>'
                     f'<text x="{left - 8:.1f}" y="{y + 4:.1f}" text-anchor="end" class="svg-mono">'
                     f'{esc(num(high - tick / 3 * (high - low)))}</text>')
    groups, segment = [], None
    for index, row in enumerate(points):
        if row['best'] is None:
            continue
        x, y = sx(index), sy(row['best'])
        if row['segment'] != segment:
            if segment is not None:
                mid = (sx(index - 1) + x) / 2
                parts.append(f'<line x1="{mid:.1f}" y1="{top:.1f}" x2="{mid:.1f}" y2="{top + plot_h:.1f}" class="svg-thresh-warn"/>'
                             f'<text x="{mid + 4:.1f}" y="{top + 11:.1f}" class="svg-muted">Segment {esc(row["segment"])}</text>')
            groups.append([(x, y)])
            segment = row['segment']
        else:
            groups[-1] += [(x, groups[-1][-1][1]), (x, y)]
    base = top + plot_h
    for group in groups:
        if len(group) > 1:
            area = [(group[0][0], base), *group, (group[-1][0], base)]
            parts.append('<polygon points="' + ' '.join(f'{x:.1f},{y:.1f}' for x, y in area) + '" class="svg-area-pass"/>')
        parts.append('<path d="M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in group) + '" class="svg-line-pass"/>')
    stride = max(1, (count + 6) // 7)
    for index, row in enumerate(points):
        x, y = sx(index), sy(row['metric'])
        tip = esc(f'Run {row["run"]}: {num(row["metric"], 8)} {unit} — {row["kind"]} — {row.get("description", "")}')
        style, radius = {'baseline': ('svg-dot-seed', 4.8), 'keep': ('svg-dot-pass', 5.8)}.get(row['kind'], ('svg-dot-discard', 3.8))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" class="{style}"><title>{tip}</title></circle>')
        if row['kind'] != 'discard' and count <= 40:
            below = row['before'] is not None and row['before'] > row['metric']
            parts.append(f'<text x="{x:.1f}" y="{y + 17 if below else max(top + 10, y - 10):.1f}" text-anchor="middle" class="svg-muted">{esc(num(row["metric"]))}</text>')
        if index in (0, count - 1) or index % stride == 0:
            parts.append(f'<text x="{x:.1f}" y="{h - 10:.1f}" text-anchor="middle" class="svg-muted">#{esc(row["run"])}</text>')
    for offset, style, label in ((4, 'svg-dot-pass', 'Keep'), (64, 'svg-dot-seed', 'Baseline'), (144, 'svg-dot-discard', 'Discard')):
        parts.append(f'<circle cx="{left + offset:.1f}" cy="11" r="4" class="{style}"/>'
                     f'<text x="{left + offset + 9:.1f}" y="15" class="svg-muted">{label}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Metric per experiment" class="chart-svg">'
            + ''.join(parts) + '</svg>')


def hypothesis_bars(items, unit):
    """One bar per reviewed hypothesis: how much of the total gain its keeps delivered."""
    total = sum(item['gain'] for item in items)
    w, row_h, left, right = 960, 34, 300.0, 250.0
    h = row_h * len(items) + 8
    parts = []
    for index, item in enumerate(items):
        y = 4 + index * row_h
        share = item['gain'] / total if total else 0
        width = (w - left - right) * share
        label = f'{item["rank"]} · {item["id"]} · {item["mechanism"]}'
        label = label if len(label) <= 44 else label[:43] + '…'
        note = ('not tried' if not item['attempts'] else
                f'{item["keeps"]} kept of {item["attempts"]}' + (f' · {num(item["gain"])} {unit} · {share * 100:.0f}%' if item['gain'] else ' · no gain'))
        parts.append(f'<text x="0" y="{y + 19}" class="{"svg-muted" if not item["attempts"] else "svg-bold"}" text-anchor="start">{esc(label)}'
                     f'<title>{esc(item["mechanism"])}</title></text>'
                     f'<rect x="{left}" y="{y + 7}" width="{w - left - right}" height="14" rx="7" style="fill:var(--border)"/>'
                     + (f'<rect x="{left}" y="{y + 7}" width="{max(width, 6):.1f}" height="14" rx="7" style="fill:var(--pass)"/>' if item['gain'] else '')
                     + f'<text x="{w - right + 12}" y="{y + 19}" class="svg-muted">{esc(note)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Gain delivered by each reviewed hypothesis" class="chart-svg">'
            + ''.join(parts) + '</svg>')


def gauge(kept, total):
    radius, around = 25.0, 2 * math.pi * 25.0
    dash = around * kept / total if total else 0
    return (f'<svg width="62" height="62" viewBox="0 0 64 64" aria-label="{kept} of {total} experiments kept">'
            f'<circle cx="32" cy="32" r="{radius}" fill="none" stroke="var(--border)" stroke-width="6"/>'
            f'<circle cx="32" cy="32" r="{radius}" fill="none" stroke="var(--pass)" stroke-width="6" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.1f} {around:.1f}" transform="rotate(-90 32 32)"/>'
            f'<text x="32" y="36" text-anchor="middle" class="svg-bold">{kept}/{total}</text></svg>')


def table(headers, rows, pill=None):
    body = ''.join('<tr>' + ''.join(
        f'<td><span class="pill {TONES.get(cell, "tone-neutral")}">{esc(cell)}</span></td>' if header == pill
        else f'<td>{esc(cell)}</td>' for header, cell in zip(headers, row)) + '</tr>' for row in rows)
    return ('<div class="table-wrap"><table><thead><tr>' + ''.join(f'<th scope="col">{esc(h)}</th>' for h in headers)
            + '</tr></thead><tbody>' + (body or f'<tr><td colspan="{len(headers)}">none</td></tr>') + '</tbody></table></div>')


def render(workspace, design=None, output_dir=None):
    workspace = Path(workspace).resolve()
    state, configs, results = load(workspace)
    hypotheses = design_hypotheses(Path(design)) if design else []
    rows = annotate(configs, results, hypotheses)
    config, segment = state['config'], state['segment']
    current = [r for r in rows if r['segment'] == segment]
    unit, lower = config.get('metricUnit') or '', config['bestDirection'] == 'lower'
    baseline = current[0]['metric'] if current else None
    best = state['best']['metric'] if state.get('best') else None
    experiments = [r for r in current[1:] if r['kind'] != 'exhausted']
    kept = sum(r['status'] == 'keep' for r in experiments)

    last = current[-1].get('description', '') if current else ''
    if (state.get('budget_reason') or '').startswith('target reached'):
        stop, tone = state['budget_reason'], 'tone-pass'
    elif last.startswith('EXHAUSTED:'):
        stop, tone = 'reviewed hypotheses exhausted', 'tone-warn'
    elif state.get('budget_reason'):
        stop, tone = state['budget_reason'], 'tone-warn'
    elif (workspace / '.autoresearch-off').exists() or (workspace / '.autoresearch-off').is_symlink():
        stop, tone = 'paused', 'tone-warn'
    else:
        stop, tone = 'in progress', 'tone-neutral'

    gain = None
    if baseline is not None and best is not None and best != baseline:
        change = best - baseline
        gain = ('+' if change > 0 else '') + num(change) + ' ' + unit
        if baseline and best:
            factor = baseline / best if lower else best / baseline
            gain += f' · {abs(change / baseline) * 100:.3g}% · {num(factor, 3)}×'
    improved = gain is not None and ((best < baseline) if lower else (best > baseline))
    target = config.get('targetMetric')
    elapsed = max(0, current[-1]['timestamp'] - config['startedAt']) if current and config.get('startedAt') else None
    runs_label = f'{len(current)}' + (f' / {config["maxRuns"]}' if config.get('maxRuns') else '')

    if baseline is None:
        metric = '<div class="big-metric"><span class="metric-old">no baseline logged</span></div>'
    else:
        metric = (f'<div class="big-metric"><span class="metric-old">{esc(num(baseline))}</span>'
                  f'<span class="metric-arrow">→</span>'
                  f'<span class="metric-new {"tone-pass" if improved else ""}">{esc(num(best))}</span>'
                  f'<span class="metric-unit">{esc(unit)} {esc(config.get("metricName"))} ({esc(config["bestDirection"])} is better)</span>'
                  + (f'<span class="delta-pill {"tone-pass" if improved else "tone-warn"}">{esc(gain)}</span>' if gain else '')
                  + '</div>')
    overview = (
        f'<div class="scoreboard"><div class="score-main">'
        f'<div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.45rem">'
        f'<span class="status-chip {tone}">{esc(stop)}</span>'
        f'<span class="goal-kicker" style="margin:0">{esc(config.get("name"))}</span></div>{metric}</div>'
        f'<div class="score-right"><div class="gauge-wrap">{gauge(kept, len(experiments))}'
        f'<div class="gauge-meta"><div class="gauge-title">Experiments kept</div>'
        f'<div class="gauge-sub">noise floor {esc(num(config.get("noiseFloor")))} {esc(unit)}</div></div></div>'
        f'<div class="mini-stat"><div class="mini-stat-val">{esc(runs_label)}</div><div class="mini-stat-lbl">Logged runs</div></div>'
        f'<div class="mini-stat"><div class="mini-stat-val">{esc(num(target)) if target is not None else "–"}</div><div class="mini-stat-lbl">Target</div></div>'
        f'<div class="mini-stat" title="First config start to last logged result"><div class="mini-stat-val">'
        f'{esc(f"{elapsed / 60:.0f} min") if elapsed is not None else "–"}</div><div class="mini-stat-lbl">Elapsed</div></div>'
        f'</div></div>'
        f'<div class="canvas-card"><div class="canvas-head"><h3 class="canvas-title">Metric per experiment</h3>'
        f'<span class="canvas-meta">{esc(unit)} · line is best kept so far</span></div>{trajectory(rows, unit)}</div>'
        f'<p style="color:var(--muted)">{esc(LIMIT)}</p>')

    by_hypothesis, bars = [], []
    for item in hypotheses + [{'rank': '–', 'id': None, 'mechanism': 'not attributed to a reviewed hypothesis'}]:
        tried = [r for r in experiments if r['hypothesis'] == item['id']]
        if item['id'] is None and not tried:
            continue
        keeps = [r for r in tried if r['status'] == 'keep']
        best_keep = (min if lower else max)((r['metric'] for r in keeps), default=None)
        item.update(attempts=len(tried), keeps=len(keeps), gain=sum(
            abs(r['before'] - r['metric']) for r in keeps if r['before'] is not None and r['best'] == r['metric'] != r['before']))
        bars.append(item)
        by_hypothesis.append([item['rank'], item['id'] or '–', item['mechanism'], len(tried), len(keeps), num(best_keep),
                              'not tried' if not tried else 'kept' if keeps else 'no gain'])
    bars_card = (f'<div class="canvas-card"><div class="canvas-head"><h3 class="canvas-title">Gain by reviewed hypothesis</h3>'
                 f'<span class="canvas-meta">share of the total improvement delivered by its keeps</span></div>'
                 f'{hypothesis_bars(bars, unit)}</div>') if design and bars else ''
    overview = overview.replace('<p style="color:var(--muted)">', bars_card + '<p style="color:var(--muted)">', 1)
    hypothesis_headers = ['Rank', 'Id', 'Mechanism', 'Attempts', 'Keeps', 'Best kept', 'Outcome']
    flow = ('<section class="report-section"><h2>Reviewed hypotheses</h2>'
            + (f'<p>From {esc(design)}. A run counts toward a hypothesis when its description names the id.</p>'
               + bars_card + table(hypothesis_headers, by_hypothesis) if design else
               '<p>No reviewed design was given; pass --design to group runs by hypothesis.</p>') + '</section>')

    run_headers = ['Run', 'Segment', 'Status', 'Metric', 'Change vs best before', 'Hypothesis', 'Commit', 'Description', 'Logged (UTC)']
    run_rows = [[r['run'], r['segment'], r['kind'], num(r['metric'], 8),
                 '–' if r['before'] is None or r['status'] in ('crash', 'checks_failed') else
                 ('+' if r['metric'] > r['before'] else '') + num(r['metric'] - r['before']),
                 r['hypothesis'] or '–', str(r.get('commit', ''))[:10], r.get('description', ''),
                 time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['timestamp'])) if r.get('timestamp') else '–'] for r in rows]
    config_headers = ['Segment', 'Name', 'Metric', 'Direction', 'Noise floor', 'Max runs', 'Max seconds', 'Target']
    config_rows = [[i, c.get('name'), f'{c.get("metricName")} ({c.get("metricUnit") or "no unit"})', c.get('bestDirection'),
                    num(c.get('noiseFloor')), c.get('maxRuns') or '–', c.get('maxSeconds') or '–',
                    num(c['targetMetric']) if c.get('targetMetric') is not None else '–'] for i, c in enumerate(configs)]
    audit = ('<section class="report-section"><h2>Every logged run</h2>' + table(run_headers, run_rows, pill='Status') + '</section>'
             '<section class="report-section"><h2>Session configuration</h2>' + table(config_headers, config_rows) + '</section>')

    provenance = f'Projected from {workspace / "autoresearch.jsonl"} at {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}. {LIMIT}'
    page = (ROOT / 'resources/improvement-report.html').read_text(encoding='utf-8')
    page = page.replace('{{provenance}}', esc(provenance)).replace('{{content}}', (
        f'<div class="view-panel panel-overview">{overview}</div>\n'
        f'<div class="view-panel panel-flow">{flow}</div>\n'
        f'<div class="view-panel panel-audit">{audit}</div>'))

    def md_table(headers, body):
        return ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers),
                *['| ' + ' | '.join(md(cell) for cell in row) + ' |' for row in body], '']
    lines = [f'# {md(config.get("name"))}', '',
             f'- Status: {md(stop)}',
             f'- {md(config.get("metricName"))} ({md(config["bestDirection"])} is better): '
             f'{num(baseline)} → {num(best)} {md(unit)}' + (f' ({md(gain)})' if gain else ''),
             f'- Logged runs: {runs_label}; experiments kept: {kept} of {len(experiments)}',
             f'- {LIMIT}', '']
    if design:
        lines += ['## Reviewed hypotheses', '', *md_table(hypothesis_headers, by_hypothesis)]
    lines += ['## Every logged run', '', *md_table(run_headers, run_rows)]

    output = Path(output_dir).resolve() if output_dir else workspace / 'experiments'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.html').write_text(page, encoding='utf-8')
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return output / 'report.html'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='optimizer report', description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path.cwd(), help='Driveline experiment worktree')
    parser.add_argument('--design', type=Path, help='reviewed design with a "## Ranked hypotheses" table')
    parser.add_argument('--output-dir', type=Path, help='default: <workspace>/experiments')
    args = parser.parse_args(argv)
    try:
        print(render(args.workspace, args.design, args.output_dir))
    except (OSError, ValueError, KeyError) as exc:
        print(f'[optimizer report] {exc}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
