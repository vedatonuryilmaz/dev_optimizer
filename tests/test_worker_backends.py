"""Exercise adapters through the real pinned supervisor with fake native CLIs."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('worker_fixture', ROOT / 'vendor/driveline-autoresearch/tests/test_codex_loop.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
spec = importlib.util.spec_from_file_location('worker_adapter', ROOT / 'scripts/optimizer_worker.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class WorkerBackendTests(unittest.TestCase):
    setUp = fixture.CodexLoopTests.setUp
    git = fixture.CodexLoopTests.git
    write_state = fixture.CodexLoopTests.write_state
    process_running = staticmethod(fixture.CodexLoopTests.process_running)

    def run_worker(self, worker, *args, mode='ok'):
        fake = fixture.FAKE_CODEX.replace(
            'if mode == "error":',
            'if mode == "json_error":\n    print(json.dumps({"type":"result", "is_error":True, "status":"ERROR", "result":"authentication failed"}))\n    sys.exit(0)\nif mode == "error":')
        fake = fake.replace('"description": "fake experiment"',
                            '"description": "EXHAUSTED: none left" if mode == "exhausted" else "fake experiment"')
        fake = fake.replace('prompt = sys.stdin.read()', "prompt = next((arg[3:] for arg in sys.argv if arg.startswith('-p=')), None) or sys.stdin.read()")
        for name in ('claude', 'agy'):
            path = self.bin / name
            path.write_text(fake)
            path.chmod(0o755)
        return subprocess.run([sys.executable, str(ROOT / 'scripts/optimizer_worker.py'),
                               '--workspace', str(self.root), '--worker', worker, *args],
                              capture_output=True, text=True, timeout=12,
                              env=dict(self.env, AR_TEST_MODE=mode))

    def test_each_backend_uses_native_protocol_model_effort_and_safe_permissions(self):
        reviewed = self.root.parent / (self.root.name + '-reviewed.md')
        reviewed.write_text('# Reviewed design\n')
        self.addCleanup(lambda: reviewed.unlink(missing_ok=True))
        for worker in ('codex', 'claude', 'agy'):
            with self.subTest(worker=worker):
                self.write_state()
                (self.root / '.autoresearch-off').unlink(missing_ok=True)
                result = self.run_worker(worker, '--max-turns', '1', '--model', 'chosen-model',
                                         '--effort', 'high', '--reviewed-design', str(reviewed))
                self.assertEqual(result.returncode, 0, result.stderr)
                call = json.loads((self.root / 'invocations').read_text().splitlines()[-1])
                self.assertIn('chosen-model', call)
                self.assertNotIn('--dangerously-skip-permissions', call)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', call)
                prompt = (self.root / '.autoresearch-test-prompt.txt').read_text()
                self.assertIn('exactly ONE experiment', prompt)
                self.assertIn('turn timeout:', prompt)
                self.assertIn(str(reviewed.resolve()), prompt)
                self.assertIn('DEFERRED', prompt)
                self.assertIn('EXHAUSTED:', prompt)
                if worker == 'codex':
                    self.assertIn('model_reasoning_effort="high"', call)
                else:
                    if worker == 'claude':
                        self.assertEqual(call[0], '-p')
                    else:
                        self.assertTrue(call[-1].startswith('-p=Use the autoresearch skill'))
                        self.assertIn('BypassSandbox:true', prompt)
                    self.assertIn('high', call)
                    self.assertNotIn('--output-last-message', call)
                if worker == 'claude':
                    settings = json.loads(call[call.index('--settings') + 1])
                    self.assertTrue(settings['disableAllHooks'])
                    self.assertFalse(settings['sandbox']['allowUnsandboxedCommands'])
                    self.assertEqual(settings['sandbox']['filesystem']['allowWrite'],
                                     [str(self.root), str(self.root / '.git')])
                elif worker == 'agy':
                    self.assertIn('--sandbox', call)
                    self.assertIn('accept-edits', call)
                    grants = [call[i + 1] for i, flag in enumerate(call) if flag == '--add-dir']
                    self.assertEqual(grants, [str(self.root / '.git')])
                    settings = json.loads(call[call.index('--settings') + 1])
                    self.assertIs(settings['enableTerminalSandbox'], True)
                    self.assertEqual(settings['toolPermission'], 'proceed-in-sandbox')
                    allowed = settings['permissions']['allow']
                    self.assertFalse(any(rule.startswith('command(') for rule in allowed))
                    self.assertIn('read_file(' + str(reviewed.resolve()) + ')', allowed)
                    self.assertEqual([rule for rule in allowed if rule.startswith('write_file(')],
                                     ['write_file(' + str(self.root) + ')',
                                      'write_file(' + str(self.root / '.git') + ')'])

    def test_dry_run_translates_without_writes(self):
        self.run_worker('claude', '--status')  # Install fixture executables first.
        before = set(self.root.rglob('*'))
        result = self.run_worker('agy', '--dry-run', '--model', 'chosen', '--effort', 'max')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['command'][0], 'agy')
        self.assertEqual(before, set(self.root.rglob('*')))
        self.assertFalse((self.root / 'invocations').exists())

    def test_external_review_inputs_get_read_only_session_rules(self):
        reviewed = self.root.parent / (self.root.name + '-reviewed.md')
        extra_dir = self.root.parent / (self.root.name + '-evidence')
        reviewed.write_text('human reviewed fixture')
        extra_dir.mkdir()
        self.addCleanup(lambda: reviewed.unlink(missing_ok=True))
        self.addCleanup(lambda: extra_dir.rmdir() if extra_dir.exists() else None)
        result = self.run_worker('claude', '--dry-run', '--reviewed-design',
                                 str(reviewed), '--read-path', str(self.root / 'autoresearch.sh'),
                                 '--read-path', str(extra_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads(result.stdout)['command']
        settings = json.loads(call[call.index('--settings') + 1])
        rules = settings['permissions']['allow']
        self.assertIn('Read(/' + str(reviewed.resolve()) + ')', rules)
        self.assertIn('Read(/' + str((self.root / 'autoresearch.sh').resolve()) + ')', rules)
        self.assertIn('Read(/' + str(extra_dir.resolve()) + '/**)', rules)
        self.assertTrue(any('skills/autoresearch/**' in rule for rule in rules))
        self.assertTrue(all(rule.startswith('Read(') for rule in rules))
        grants = [call[i + 1] for i, arg in enumerate(call) if arg == '--add-dir']
        self.assertEqual(grants, [str(self.root / '.git')])

    def test_default_codex_command_is_unchanged(self):
        command = ['codex', 'exec', '--cd', '/tmp/work', '--sandbox', 'workspace-write', '-']
        self.assertEqual(adapter.worker_command(command), command)

    def test_native_zero_exit_error_is_failure_with_original_evidence(self):
        result = self.run_worker('agy', mode='json_error')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('agy returned an error result', result.stderr)
        events = list((self.root / 'experiments/codex').glob('*/events.jsonl'))
        self.assertEqual(len(events), 1)
        self.assertIn('authentication failed', events[0].read_text())
        self.assertTrue((self.root / '.autoresearch-off').exists())

    def test_native_postchecks_reject_missing_double_locked_and_dirty_changes(self):
        for mode in ('none', 'double', 'harness', 'dirty', 'config', 'error'):
            with self.subTest(mode=mode):
                self.write_state()
                (self.root / 'autoresearch.sh').write_text('scorer')
                (self.root / 'code.txt').write_text('original')
                (self.root / '.autoresearch-off').unlink(missing_ok=True)
                (self.root / 'invocations').unlink(missing_ok=True)
                result = self.run_worker('claude', mode=mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(len((self.root / 'invocations').read_text().splitlines()), 1)
                self.assertTrue((self.root / '.autoresearch-off').exists())

    def test_exhausted_reviewed_design_pauses_instead_of_spending_turns(self):
        reviewed = self.root.parent / (self.root.name + '-exhausted.md')
        reviewed.write_text('# Reviewed design\n')
        self.addCleanup(lambda: reviewed.unlink(missing_ok=True))
        for design, turns in ((('--reviewed-design', str(reviewed)), 1), ((), 3)):
            with self.subTest(design=design):
                self.write_state()
                (self.root / '.autoresearch-off').unlink(missing_ok=True)
                (self.root / 'invocations').unlink(missing_ok=True)
                result = self.run_worker('claude', '--max-turns', '3', *design, mode='exhausted')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len((self.root / 'invocations').read_text().splitlines()), turns)
                self.assertEqual('reviewed hypotheses exhausted' in result.stderr, bool(design))

    def test_reviewed_design_resolves_relative_path_and_clamps_turn_budget(self):
        caller_dir = self.root.parent / (self.root.name + '-caller')
        caller_dir.mkdir()
        design = caller_dir / 'design.md'
        design.write_text('# Design\n')
        self.addCleanup(lambda: (design.unlink(missing_ok=True), caller_dir.rmdir() if caller_dir.exists() else None))
        self.write_state()
        self.run_worker('codex', '--status')  # ensure fake executables exist
        res = subprocess.run([sys.executable, str(ROOT / 'scripts/optimizer_worker.py'),
                              '--workspace', str(self.root), '--worker', 'codex', '--max-turns', '1',
                              '--turn-timeout', '900', '--max-seconds', '42', '--reviewed-design', 'design.md'],
                             cwd=caller_dir, capture_output=True, text=True, timeout=12, env=dict(self.env, AR_TEST_MODE='ok'))
        self.assertEqual(res.returncode, 0, res.stderr)
        prompt = (self.root / '.autoresearch-test-prompt.txt').read_text()
        self.assertIn(str(design.resolve()), prompt)
        self.assertIn('DEFERRED', prompt)
        self.assertIn('EXHAUSTED:', prompt)
        import re
        seconds = int(re.search(r'turn timeout: (\d+)s', prompt).group(1))
        self.assertLessEqual(seconds, 42)
        self.assertGreaterEqual(seconds, 1)

        missing = self.run_worker('codex', '--max-turns', '1', '--reviewed-design', 'no-such-design.md')
        self.assertEqual(missing.returncode, 2)
        self.assertIn('--reviewed-design must be an existing regular file', missing.stderr)

    def test_timeout_reaps_worker(self):
        # Long enough for the fake worker's interpreter to start and record its pid.
        result = self.run_worker('agy', '--turn-timeout', '3', mode='sleep')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('budget expired', result.stderr)
        pid = int((self.root / '.autoresearch-test-parent.pid').read_text())
        self.assertFalse(self.process_running(pid))

    def test_success_reaps_sigterm_ignoring_descendant(self):
        child_pid = None
        try:
            started = time.monotonic()
            result = self.run_worker('claude', '--max-turns', '1', mode='descendant_exit')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 8)
            child_pid = int((self.root / '.autoresearch-test-child.pid').read_text())
            self.assertFalse(self.process_running(child_pid))
        finally:
            if child_pid and self.process_running(child_pid):
                os.kill(child_pid, signal.SIGKILL)

    def test_help_prints_driveline_and_host_egm_skill_paths(self):
        result = self.run_worker('codex', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(line.split(': ', 1) for line in result.stdout.splitlines()
                     if line.startswith(('Driveline skill: ', 'EGM skills: ', 'EGM skills (claude): ')))
        self.assertTrue(Path(lines['Driveline skill']).is_file())
        for key in ('EGM skills', 'EGM skills (claude)'):
            skills = Path(lines[key])
            for name in ('egm-brainstorm', 'egm-draft', 'egm-review', 'egm-resolve'):
                self.assertTrue((skills / name / 'SKILL.md').is_file())
            self.assertTrue((skills.parent / 'agents').is_dir())
            self.assertTrue((skills.parent / 'resources/document_standards').is_dir())

    def test_error_status_and_subtype_are_detected(self):
        log_dir = self.root / 'test-log'
        log_dir.mkdir()
        for event in ({'type': 'result', 'status': 'ERROR'},
                      {'type': 'result', 'subtype': 'error_during_execution'},
                      {'type': 'error', 'message': 'provider unavailable'}):
            (log_dir / 'events.jsonl').write_text(json.dumps(event) + '\n')
            self.assertTrue(adapter.terminal_result(log_dir)[1])


if __name__ == '__main__':
    unittest.main()


def test_agy_nested_terminal_success_can_retain_recoverable_denial(tmp_path):
    (tmp_path / "events.jsonl").write_text(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}}) + "\n")
    result, failed = adapter.terminal_result(tmp_path)
    assert not failed
    assert result["denied_actions"]


def test_agy_nested_terminal_success_keeps_response(tmp_path):
    (tmp_path / "events.jsonl").write_text(json.dumps({"event": "result", "result": {
        "status": "SUCCESS", "response": "Completed exactly one experiment"}}) + "\n")
    result, failed = adapter.terminal_result(tmp_path)
    assert not failed
    assert result["response"] == "Completed exactly one experiment"


def test_agy_nested_terminal_failed_status_is_a_failure(tmp_path):
    (tmp_path / "events.jsonl").write_text(json.dumps({"event": "result", "result": {
        "status": "FAILED", "denied_actions": [{"action": "command"}]}}) + "\n")
    result, failed = adapter.terminal_result(tmp_path)
    assert failed
    assert result["status"] == "FAILED"


def test_transient_midstream_error_does_not_fail_terminal_success(tmp_path):
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"type": "error", "message": "transient retry"}) + "\n"
        + json.dumps({"type": "result", "status": "SUCCESS", "result": "completed"}) + "\n"
    )
    result, failed = adapter.terminal_result(tmp_path)
    assert not failed
    assert result["result"] == "completed"


def test_sandbox_safe_env_strips_only_external_virtualenv(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ext = tmp_path / "external-venv"
    ws.mkdir()
    (ext / "bin").mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(ext))
    monkeypatch.setenv("PATH", f"{ext / 'bin'}{os.pathsep}/usr/bin")
    with adapter._sandbox_safe_env(ws):
        assert "VIRTUAL_ENV" not in os.environ
        assert str(ext / "bin") not in os.environ["PATH"].split(os.pathsep)
        assert "/usr/bin" in os.environ["PATH"].split(os.pathsep)
    assert os.environ["VIRTUAL_ENV"] == str(ext)
    assert str(ext / "bin") in os.environ["PATH"].split(os.pathsep)

    local = ws / ".venv"
    (local / "bin").mkdir(parents=True)
    monkeypatch.setenv("VIRTUAL_ENV", str(local))
    monkeypatch.setenv("PATH", f"{local / 'bin'}{os.pathsep}/usr/bin")
    with adapter._sandbox_safe_env(ws):
        assert os.environ["VIRTUAL_ENV"] == str(local)
        assert str(local / "bin") in os.environ["PATH"].split(os.pathsep)
