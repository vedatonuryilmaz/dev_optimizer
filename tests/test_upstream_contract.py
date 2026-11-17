"""Offline integrity of pinned upstreams and explicitly declared local patches."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "upstreams.lock.json").read_text())


class UpstreamContractTests(unittest.TestCase):
    def test_pinned_trees_match_every_upstream_blob_and_license(self):
        self.assertEqual(LOCK["schema_version"], 1)
        for name, item in LOCK["upstreams"].items():
            with self.subTest(upstream=name):
                root = ROOT / item["path"]
                self.assertRegex(item["revision"], r"^[0-9a-f]{40}$")
                self.assertEqual(item["file_count"], len(item["files"]))
                actual_files = {
                    str(path.relative_to(root)) for path in root.rglob("*")
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and ".pytest_cache" not in path.parts
                }
                self.assertEqual(actual_files, set(item["files"]))
                records = []
                effective_records = []
                effective_files = item.get("effective_files", {})
                self.assertLessEqual(set(effective_files), set(item["files"]))
                for relative, digest in sorted(item["files"].items()):
                    path = root / relative
                    self.assertFalse(path.is_symlink(), relative)
                    effective_digest = effective_files.get(relative, digest)
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), effective_digest, relative)
                    executable = bool(path.stat().st_mode & 0o111)
                    self.assertEqual(executable, item["file_modes"][relative] == "100755", relative)
                    records.append(f"{relative}\0{item['file_modes'][relative]}\0{digest}\n")
                    effective_records.append(f"{relative}\0{item['file_modes'][relative]}\0{effective_digest}\n")
                self.assertEqual(hashlib.sha256("".join(records).encode()).hexdigest(), item["tree_sha256"])
                self.assertEqual(item["files"][item["license_file"]], item["license_sha256"])
                if effective_files:
                    self.assertEqual(hashlib.sha256("".join(effective_records).encode()).hexdigest(),
                                     item["effective_tree_sha256"])

    def test_declared_patch_reconstructs_pristine_upstream_and_effective_tree(self):
        for name, item in LOCK["upstreams"].items():
            if "patch_path" not in item:
                self.assertNotIn("effective_files", item)
                continue
            with self.subTest(upstream=name), tempfile.TemporaryDirectory() as temporary:
                patch = (ROOT / item["patch_path"]).read_bytes()
                self.assertEqual(hashlib.sha256(patch).hexdigest(), item["patch_sha256"])
                root = Path(temporary)
                effective = {}
                for relative in item["files"]:
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    effective[relative] = (ROOT / item["path"] / relative).read_bytes()
                    path.write_bytes(effective[relative])
                    path.chmod(0o755 if item["file_modes"][relative] == "100755" else 0o644)
                # Reverse the declared patch, then check every original pinned
                # blob; reapply it to prove exact reconstruction in both directions.
                for reverse in (True, False):
                    command = ["git", "apply", "--no-index", "--whitespace=error"]
                    if reverse:
                        command.append("--reverse")
                    result = subprocess.run(command, input=patch, cwd=root, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
                    self.assertEqual(actual, set(item["files"]))
                    for relative, digest in item["files"].items():
                        contents = (root / relative).read_bytes()
                        if reverse:
                            self.assertEqual(hashlib.sha256(contents).hexdigest(), digest, relative)
                        else:
                            self.assertEqual(contents, effective[relative], relative)

    def test_native_state_roundtrip_and_pause_contract(self):
        script = ROOT / LOCK["upstreams"]["driveline"]["path"] / "skills/autoresearch/scripts/ar_state.py"
        spec = importlib.util.spec_from_file_location("upstream_contract_ar_state", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "autoresearch.jsonl"
            module.init_state(path, {"name": "bridge fixture", "metricName": "latency", "metricUnit": "ms", "bestDirection": "lower", "maxRuns": 2})
            row = module.append_result(path, run="auto", commit="a" * 40, metric=1.0, status="keep", segment="auto", description="native compatibility")
            summary = module.parse_state(path.read_text())
            self.assertEqual(summary["best"], row)
            self.assertEqual(summary["total_runs"], 1)
            self.assertFalse(summary["budget_reached"])
            with self.assertRaises(module.StateError):
                module.parse_state(path.read_text().replace('"metric":1.0', '"metric":NaN'))


if __name__ == "__main__":
    unittest.main()
