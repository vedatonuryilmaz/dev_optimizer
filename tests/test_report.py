"""The report projects a real Driveline ledger; it measures nothing itself."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = load("report_ar_state", "vendor/driveline-autoresearch/skills/autoresearch/scripts/ar_state.py")
report = load("report_module", "scripts/optimizer_report.py")
worker = load("report_worker", "scripts/optimizer_worker.py")


def ledger(root, rows, **config):
    path = root / "autoresearch.jsonl"
    state.init_state(path, {"name": "Parser speed", "metricName": "runtime", "metricUnit": "s",
                            "bestDirection": "lower", "noiseFloor": 0.1, "maxRuns": 20, **config})
    for metric, status, description in rows:
        state.append_result(path, run="auto", commit="abc1234", metric=metric, status=status,
                            segment="auto", description=description)


def test_report_shows_gain_and_attributes_runs_to_reviewed_hypotheses(tmp_path):
    ledger(tmp_path, [(12.0, "keep", "baseline"), (9.0, "keep", "H1: cache tokens"),
                      (9.5, "discard", "H1: second attempt <script>alert(1)</script>"),
                      (0, "crash", "H2: broke the build"),
                      (4.0, "keep", "H2: skip re-parse (unblocks H1 follow-up)"),
                      (4.0, "discard", "EXHAUSTED: H1 and H2 settled, nothing left")])
    design = tmp_path / "design.md"
    design.write_text("# Design\n\n## Ranked hypotheses\n\n| rank | id | mechanism | evidence |\n|---|---|---|---|\n"
                      "| 1 | H1 | cache tokens | 40% |\n| 2 | H2 | skip re-parse | 30% |\n| 3 | H3 | arena | 5% |\n\n## Next\n")
    assert worker.main(["report", "--workspace", str(tmp_path), "--design", str(design)]) == 0
    page = (tmp_path / "experiments/report.html").read_text()
    text = (tmp_path / "experiments/report.md").read_text()
    assert "reviewed hypotheses exhausted" in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "12 → 4 s (-8 s · 66.7% · 3×)" in text
    assert "experiments kept: 2 of 4" in text
    assert "| 1 | H1 | cache tokens | 2 | 1 | 9 | kept |" in text
    assert "| 2 | H2 | skip re-parse | 2 | 1 | 4 | kept |" in text
    assert "| 3 | H3 | arena | 0 | 0 | – | not tried |" in text
    assert "independently re-measured" in text


def test_report_scopes_hypothesis_summary_and_elapsed_to_current_segment(tmp_path):
    path = tmp_path / "autoresearch.jsonl"
    state.init_state(path, {"name": "Seg0", "metricName": "score", "metricUnit": "", "bestDirection": "higher", "startedAt": 100})
    state.append_result(path, run="auto", commit="abc1234", metric=0.5, status="keep", segment="auto", description="baseline")
    state.append_result(path, run="auto", commit="abc1234", metric=0.8, status="keep", segment="auto", description="H1: old metric")
    state.append_result(path, run="auto", commit="abc1234", metric=0.8, status="discard", segment="auto", description="EXHAUSTED: done")
    state.init_state(path, {"name": "Seg1", "metricName": "runtime", "metricUnit": "s", "bestDirection": "lower", "startedAt": 50000}, new_segment=True)
    design = tmp_path / "design.md"
    design.write_text("# Design\n\n## Detailed Implementation Plan\n\n### Ranked hypotheses\n\n| rank | id | mechanism |\n|---|---|---|\n| 1 | H1 | fast path |\n")
    assert report.main(["--workspace", str(tmp_path)]) == 0
    text = (tmp_path / "experiments/report.md").read_text()
    assert "Status: in progress" in text
    state.append_result(path, run="auto", commit="abc1234", metric=10.0, status="keep", segment="auto", description="baseline")
    assert report.main(["--workspace", str(tmp_path), "--design", str(design)]) == 0
    text = (tmp_path / "experiments/report.md").read_text()
    assert "| 1 | H1 | fast path | 0 | 0 | – | not tried |" in text


def test_leading_token_attribution_and_deferred_discard_without_zero_spike(tmp_path):
    ledger(tmp_path, [(12.0, "keep", "baseline"),
                      (9.0, "keep", "H1: cache tokens"),
                      (9.0, "discard", "H1 DEFERRED (blocked on H2)"),
                      (9.2, "discard", "`H1`: retry with wrapper"),
                      (9.1, "discard", "H1.1: dotted id"),
                      (9.3, "discard", "H2. trailing dot"),
                      (9.4, "discard", "Trying H2 without leading id"),
                      (9.0, "discard", "EXHAUSTED: H1 and H2 settled")])
    design = tmp_path / "design.md"
    design.write_text("# Design\n\n## Ranked hypotheses\n\n| rank | id | mechanism |\n|---|---|---|\n"
                      "| 1 | H1 | cache tokens |\n| 2 | H2 | skip re-parse |\n| 3 | H1.1 | sub-step |\n")
    _, configs, results = report.load(tmp_path)
    annotated = report.annotate(configs, results, report.design_hypotheses(design))
    assert [r["hypothesis"] for r in annotated] == [None, "H1", "H1", "H1", "H1.1", "H2", None, None]

    assert report.main(["--workspace", str(tmp_path), "--design", str(design)]) == 0
    page = (tmp_path / "experiments/report.html").read_text()
    text = (tmp_path / "experiments/report.md").read_text()
    assert "| 1 | H1 | cache tokens | 3 | 1 | 9 | kept |" in text
    assert "| 2 | H2 | skip re-parse | 1 | 0 | – | no gain |" in text
    assert "| 3 | H1.1 | sub-step | 1 | 0 | – | no gain |" in text
    assert "| – | – | not attributed to a reviewed hypothesis | 1 | 0 | – | no gain |" in text
    assert "| 3 | 0 | discard | 9 | 0 | H1 |" in text
    assert "| 8 | 0 | exhausted | 9 | 0 | – |" in text
    assert ": 0 s —" not in page
    assert '>0</text>' not in page


def test_report_without_design_or_gain_and_with_target_reached(tmp_path):
    ledger(tmp_path, [(5.0, "keep", "baseline"), (7.0, "keep", "more throughput")],
           bestDirection="higher", targetMetric=6.0)
    assert report.main(["--workspace", str(tmp_path), "--output-dir", str(tmp_path / "out")]) == 0
    text = (tmp_path / "out/report.md").read_text()
    assert "Status: target reached" in text
    assert "5 → 7 s (+2 s · 40% · 1.4×)" in text
    assert "Reviewed hypotheses" not in text
    assert "pass --design" in (tmp_path / "out/report.html").read_text()
    assert report.main(["--workspace", str(tmp_path / "missing")]) == 2
