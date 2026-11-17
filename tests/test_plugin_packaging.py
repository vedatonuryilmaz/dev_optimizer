"""The plugin, turnkey host installer, and sdist/wheel bundle stay aligned."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import hatchling.build

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/optimizer"
WORKER = ROOT / "scripts/optimizer_worker.py"


def _make_fake_hosts(bin_dir: Path, log: Path, hosts=("claude", "codex", "agy")):
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = (
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(log)!r})\n"
        "with log.open('a') as f:\n"
        "    f.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
    )
    for host in hosts:
        exe = bin_dir / host
        exe.write_text(script)
        exe.chmod(0o755)


def test_plugin_identity():
    portable = json.loads((PLUGIN / "plugin.json").read_text())
    claude = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert portable == claude
    assert portable["name"] == "optimizer"
    assert [p.name for p in (PLUGIN / "skills").iterdir()] == ["optimizer"]
    skill_text = (PLUGIN / "skills/optimizer/SKILL.md").read_text()
    assert skill_text.startswith("---\nname: optimizer\n")
    import importlib.util, re, tempfile
    header = re.search(r"`(rank\s*\|\s*id\s*\|\s*mechanism[^`]*)`", skill_text).group(1)
    cols = [c.strip() for c in header.split("|")]
    sample_row = ["1", "H1", "cache tokens", *(["x"] * (len(cols) - 3))]
    spec = importlib.util.spec_from_file_location("pkg_report_check", ROOT / "scripts/optimizer_report.py")
    report_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report_mod)
    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "design.md"
        doc.write_text("## Ranked hypotheses\n\n| " + " | ".join(cols) + " |\n|"
                       + "---|" * len(cols) + "\n| " + " | ".join(sample_row) + " |\n")
        assert report_mod.design_hypotheses(doc) == [{"rank": "1", "id": "H1", "mechanism": "cache tokens"}]


def test_optimizer_install_wires_claude_codex_and_agy_idempotently(tmp_path):
    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    log = tmp_path / "calls.jsonl"
    _make_fake_hosts(bin_dir, log)
    # Dangling symlink must be replaced cleanly on install.
    (home / ".claude/skills").mkdir(parents=True)
    (home / ".claude/skills/optimizer").symlink_to(tmp_path / "stale-missing-target")
    env = {**os.environ, "HOME": str(home), "PATH": str(bin_dir)}

    for _ in range(2):
        res = subprocess.run([sys.executable, str(WORKER), "install"],
                             env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    assert (home / ".claude/skills/optimizer").resolve() == (PLUGIN / "skills/optimizer").resolve()
    assert (home / ".agents/skills/optimizer").resolve() == (PLUGIN / "skills/optimizer").resolve()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    egm = str(ROOT / "vendor/elephant-goldfish-model")
    assert ["claude", "plugin", "marketplace", "add", egm] in calls
    assert ["claude", "plugin", "install", "egm@egm"] in calls
    assert ["codex", "plugin", "marketplace", "add", egm] in calls
    assert ["codex", "plugin", "add", "egm@egm"] in calls
    assert ["agy", "plugin", "install", str(PLUGIN)] in calls
    assert ["agy", "plugin", "install", egm] in calls

    # Refuses to clobber a non-symlink user directory, and fails cleanly with no hosts.
    (home / ".claude/skills/optimizer").unlink()
    (home / ".claude/skills/optimizer").mkdir()
    blocked = subprocess.run([sys.executable, str(WORKER), "install", "--host", "claude"],
                             env=env, capture_output=True, text=True)
    assert blocked.returncode == 2
    assert "non-symlink" in blocked.stderr

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    no_host = subprocess.run([sys.executable, str(WORKER), "install"],
                             env={**os.environ, "HOME": str(home), "PATH": str(empty_bin)},
                             capture_output=True, text=True)
    assert no_host.returncode == 2
    assert "no supported host CLI" in no_host.stderr


def test_sdist_to_wheel_bundle_supports_non_editable_git_install(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    orig_cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        sdist_name = hatchling.build.build_sdist(str(dist))
    finally:
        os.chdir(orig_cwd)

    sdist_unpack = tmp_path / "sdist"
    with tarfile.open(dist / sdist_name) as tar:
        tar.extractall(sdist_unpack, filter="data")
    (sdist_root,) = [p for p in sdist_unpack.iterdir() if p.is_dir()]

    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    try:
        os.chdir(sdist_root)
        wheel_name = hatchling.build.build_wheel(str(wheel_dir))
    finally:
        os.chdir(orig_cwd)

    site_packages = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel_dir / wheel_name) as whl:
        names = set(whl.namelist())
        ar_log = whl.getinfo("vendor/driveline-autoresearch/skills/autoresearch/scripts/ar-log.sh")
        assert (ar_log.external_attr >> 16) & 0o111
        for required in (
            "plugins/optimizer/plugin.json",
            "plugins/optimizer/.claude-plugin/plugin.json",
            "plugins/optimizer/skills/optimizer/SKILL.md",
            "resources/improvement-report.html",
            "vendor/driveline-autoresearch/examples/autoresearch.sh",
            "vendor/elephant-goldfish-model/plugin.json",
            "vendor/elephant-goldfish-model/.claude-plugin/marketplace.json",
            "vendor/elephant-goldfish-model/.codex-plugin/plugin.json",
            "vendor/elephant-goldfish-model/.agents/plugins/marketplace.json",
            "vendor/elephant-goldfish-model/distributions/claude/.claude-plugin/plugin.json",
        ):
            assert required in names, required
        assert whl.read("resources/improvement-report.html") == (ROOT / "resources/improvement-report.html").read_bytes()
        assert whl.read("plugins/optimizer/skills/optimizer/SKILL.md") == (PLUGIN / "skills/optimizer/SKILL.md").read_bytes()
        assert whl.read("vendor/driveline-autoresearch/skills/autoresearch/scripts/codex_loop.py") == (
            ROOT / "vendor/driveline-autoresearch/skills/autoresearch/scripts/codex_loop.py"
        ).read_bytes()
        assert not any("__pycache__" in n or ".pytest_cache" in n for n in names)
        whl.extractall(site_packages)

    installed_worker = site_packages / "scripts/optimizer_worker.py"
    report_ws = tmp_path / "report-ws"
    report_ws.mkdir()
    ar_state = site_packages / "vendor/driveline-autoresearch/skills/autoresearch/scripts/ar_state.py"
    assert subprocess.run(
        [sys.executable, str(ar_state), "init", "--path", str(report_ws / "autoresearch.jsonl"),
         "--name", "wheel-check", "--metric-name", "runtime", "--metric-unit", "s", "--direction", "lower"],
        capture_output=True, text=True,
    ).returncode == 0
    report_res = subprocess.run([sys.executable, str(installed_worker), "report", "--workspace", str(report_ws)],
                                capture_output=True, text=True)
    assert report_res.returncode == 0, report_res.stderr
    assert (report_ws / "experiments/report.html").is_file()
    help_res = subprocess.run([sys.executable, str(installed_worker), "--help"],
                              capture_output=True, text=True)
    assert help_res.returncode == 0, help_res.stderr
    assert str(site_packages / "vendor/elephant-goldfish-model/skills") in help_res.stdout
    assert str(site_packages / "vendor/elephant-goldfish-model/distributions/claude/skills") in help_res.stdout

    bin_dir = tmp_path / "bin"
    home = tmp_path / "home"
    log = tmp_path / "wheel-calls.jsonl"
    _make_fake_hosts(bin_dir, log)
    inst_res = subprocess.run(
        [sys.executable, str(installed_worker), "install"],
        env={**os.environ, "HOME": str(home), "PATH": str(bin_dir)},
        capture_output=True, text=True,
    )
    assert inst_res.returncode == 0, inst_res.stderr
    assert (home / ".claude/skills/optimizer").resolve() == (site_packages / "plugins/optimizer/skills/optimizer").resolve()
    assert (home / ".agents/skills/optimizer").resolve() == (site_packages / "plugins/optimizer/skills/optimizer").resolve()
