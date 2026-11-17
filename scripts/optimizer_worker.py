#!/usr/bin/env python3
"""Run the pinned Driveline supervisor with a Codex, Claude or agy worker.

The supervisor owns all state, locks, budgets, prompts, checks and process groups.
This module changes only its worker argv and reads native structured failures.
Every other flag is the supervisor's own.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = ROOT / 'vendor/driveline-autoresearch/skills/autoresearch/scripts/codex_loop.py'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
HOSTS = ('claude', 'codex', 'agy')


def load_supervisor():
    spec = importlib.util.spec_from_file_location('_optimizer_driveline_worker', SUPERVISOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker_command(command, worker='codex', effort=None, read_paths=()):
    """Translate the upstream Codex argv, preserving its workspace/model/grants."""
    command = list(command)
    if worker == 'codex':
        if effort:
            command[2:2] = ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        return command
    root = command[command.index('--cd') + 1]
    model = command[command.index('--model') + 1] if '--model' in command else None
    grants = [command[i + 1] for i, value in enumerate(command) if value == '--add-dir']
    if worker == 'claude':
        # Ignore inherited user/project hooks and permissions. Bash remains in
        # Claude's native sandbox; no unsandboxed fallback can be approved.
        settings = {'disableAllHooks': True,
                    'permissions': {'allow': ['Read(/' + str(path) + ')' for path in read_paths]},
                    'sandbox': {'enabled': True, 'autoAllowBashIfSandboxed': True,
                                'allowUnsandboxedCommands': False,
                                'filesystem': {'allowWrite': [root, *grants]}}}
        result = ['claude', '-p', '--input-format', 'text', '--output-format', 'stream-json',
                  '--verbose', '--no-session-persistence', '--setting-sources', '',
                  '--settings', json.dumps(settings), '--permission-mode', 'acceptEdits',
                  '--permission-prompts', 'none', '--strict-mcp-config']
    elif worker == 'agy':
        # proceed-in-sandbox auto-allows commands inside Seatbelt while leaving
        # BypassSandbox:true ungranted (auto-denied in headless print mode).
        runtime_reads = list(dict.fromkeys([str(ROOT), str(Path(sys.prefix).resolve()),
                                            str(Path(sys.base_prefix).resolve())]))
        settings = {'enableTerminalSandbox': True,
                    'toolPermission': 'proceed-in-sandbox',
                    'permissions': {'allow':
                    ['read_file(' + value + ')' for value in runtime_reads] +
                    ['write_file(' + value + ')' for value in [root, *grants]] +
                    ['read_file(' + str(value).removesuffix('/**') + ')' for value in read_paths]}}
        result = ['agy', '--input-format', 'text', '--output-format', 'stream-json',
                  '--mode', 'accept-edits', '--sandbox', '--settings', json.dumps(settings)]
    else:
        raise ValueError('unknown worker: ' + worker)
    for path in grants:
        result += ['--add-dir', path]
    if model:
        result += ['--model', model]
    if effort:
        result += ['--effort', effort]
    if worker == 'agy':
        # AGY requires an attached print argument; run_turn fills the prompt.
        result.append('-p=')
    return result


def terminal_result(log_dir):
    """Read native terminal records, without treating tool/user content as status."""
    final, error = None, None
    with (log_dir / 'events.jsonl').open(errors='replace') as source:
        for line in source:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get('type', event.get('event', ''))
            if kind == 'result':
                final = event.get('result') if isinstance(event.get('result'), dict) else event
                error = None
            elif kind == 'error':
                error = event
    if error is not None:
        return error, True
    failed = bool(final and (final.get('is_error') or
                            str(final.get('subtype', '')).lower().startswith('error') or
                            str(final.get('status', '')).lower() in ('error', 'failed')))
    return final, failed


@contextlib.contextmanager
def _sandbox_safe_env(root):
    """Drop an out-of-workspace host VIRTUAL_ENV so sandboxed python3 and ar-log.sh work."""
    venv = os.environ.get('VIRTUAL_ENV')
    if not venv or Path(venv).resolve().is_relative_to(Path(root).resolve()):
        yield
        return
    blocked = {str(Path(venv) / 'bin'), str(Path(venv).resolve() / 'bin')}
    old_path = os.environ.get('PATH', '')
    os.environ.pop('VIRTUAL_ENV', None)
    os.environ['PATH'] = os.pathsep.join(entry for entry in old_path.split(os.pathsep) if entry not in blocked)
    try:
        yield
    finally:
        os.environ['VIRTUAL_ENV'] = venv
        os.environ['PATH'] = old_path


def install_hosts(argv, home=None):
    """Install the optimizer skill and pinned EGM plugin for Claude, Codex and agy."""
    parser = argparse.ArgumentParser(prog='optimizer install', description=install_hosts.__doc__)
    parser.add_argument('--host', action='append', choices=HOSTS,
                        help='host to configure (repeatable; default: every installed host CLI on PATH)')
    args = parser.parse_args(argv)
    hosts = list(dict.fromkeys(args.host or [host for host in HOSTS if shutil.which(host)]))
    if not hosts:
        print('optimizer install: no supported host CLI found on PATH (claude, codex, agy)', file=sys.stderr)
        return 2
    missing = [host for host in hosts if not shutil.which(host)]
    if missing:
        print('optimizer install: host CLI not found on PATH: ' + ', '.join(missing), file=sys.stderr)
        return 2
    home = Path.home() if home is None else Path(home)
    skill = ROOT / 'plugins/optimizer/skills/optimizer'
    plugin = ROOT / 'plugins/optimizer'
    egm = ROOT / 'vendor/elephant-goldfish-model'

    def link(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            raise RuntimeError(f'refusing to overwrite existing non-symlink: {dest}')
        dest.symlink_to(skill)

    def run(cmd, check=True):
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(f"{' '.join(cmd)} failed" + (f': {detail}' if detail else ''))

    try:
        for host in hosts:
            if host == 'claude':
                link(home / '.claude/skills/optimizer')
                run(['claude', 'plugin', 'uninstall', 'egm@egm'], check=False)
                run(['claude', 'plugin', 'marketplace', 'remove', 'egm'], check=False)
                run(['claude', 'plugin', 'marketplace', 'add', str(egm)])
                run(['claude', 'plugin', 'install', 'egm@egm'])
                run(['claude', 'plugin', 'enable', 'egm@egm'], check=False)
            elif host == 'codex':
                link(home / '.agents/skills/optimizer')
                run(['codex', 'plugin', 'remove', 'egm@egm'], check=False)
                run(['codex', 'plugin', 'marketplace', 'remove', 'egm'], check=False)
                run(['codex', 'plugin', 'marketplace', 'add', str(egm)])
                run(['codex', 'plugin', 'add', 'egm@egm'])
            elif host == 'agy':
                for name, target in (('optimizer', plugin), ('egm', egm)):
                    run(['agy', 'plugin', 'uninstall', name], check=False)
                    run(['agy', 'plugin', 'install', str(target)])
                    run(['agy', 'plugin', 'enable', name], check=False)
            print(f'installed optimizer + egm for {host}')
    except RuntimeError as exc:
        print(f'optimizer install: {exc}', file=sys.stderr)
        return 2
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['install']:
        return install_hosts(argv[1:])
    if argv[:1] == ['report']:
        sys.path.insert(0, str(Path(__file__).parent))
        import optimizer_report
        return optimizer_report.main(argv[1:])
    supervisor = load_supervisor()
    upstream_parser = supervisor.parser

    def parser():
        result = upstream_parser()
        egm = ROOT / 'vendor/elephant-goldfish-model'
        result.description = (
            __doc__
            + '\nInstall: optimizer install [--host claude|codex|agy]'
            + '\nReport: optimizer report --workspace DIR [--design PATH]'
            + '\nDriveline skill: ' + str(SUPERVISOR.parents[1] / 'SKILL.md')
            + '\nEGM skills: ' + str(egm / 'skills')
            + '\nEGM skills (claude): ' + str(egm / 'distributions/claude/skills')
        )
        result.formatter_class = argparse.RawDescriptionHelpFormatter
        result.add_argument('--worker', choices=('codex', 'claude', 'agy'), default='codex')
        result.add_argument('--effort', choices=EFFORTS)
        result.add_argument('--read-path', action='append', default=[], type=Path,
                            help='additional reviewed input allowed through the native Read tool')
        return result

    supervisor.parser = parser
    args = parser().parse_args(argv)
    original_run_turn = supervisor.run_turn
    # The authoritative skill and reviewed inputs live outside the worker tree.
    # Grant Read only: --add-dir would also expand the file-edit workspace.
    def _read_rule(path):
        resolved = Path(path).resolve()
        return str(resolved) + ('/**' if resolved.is_dir() else '')

    read_paths = [str(SUPERVISOR.parents[1]) + '/**',
                  *[_read_rule(path) for path in args.read_path]]
    if args.reviewed_design:
        read_paths.append(str(Path(args.reviewed_design).resolve()))

    def run_turn(command, prompt, root, log_dir, timeout):
        translated = worker_command(command, args.worker, args.effort, read_paths)
        if args.worker == 'claude':
            prompt += (' This is a noninteractive native-permission session: use literal paths and '
                       'literal run/segment/commit values in shell commands, without shell variables '
                       'or command substitution, which can require unavailable approval.')
        if args.worker == 'agy':
            state_script = sys.executable + ' ' + str(SUPERVISOR.parent / 'ar_state.py')
            prompt += (' Native sandbox command guidance: use ' + state_script + ' status for read-only '
                       'status; run the locked scripts as ./autoresearch.sh and ./checks.sh; log results '
                       'with the pinned ar-log.sh (or ' + state_script + ' append). Never request '
                       'BypassSandbox:true or another permission escalation; if denied, stop '
                       'without retrying outside the sandbox.')
            translated[-1] += prompt
            prompt = ''
        try:
            with _sandbox_safe_env(root):
                original_run_turn(translated, prompt, root, log_dir, timeout)
        except supervisor.LoopError as exc:
            # Keep the native stderr and events intact for recovery classification.
            raise supervisor.LoopError(f'{args.worker}: {exc}') from exc
        if args.worker != 'codex':
            result, failed = terminal_result(log_dir)
            if result is not None:
                (log_dir / 'final.md').write_text(str(result.get('result', result.get('response', json.dumps(result)))) + '\n')
            if failed:
                raise supervisor.LoopError(f'{args.worker} returned an error result; inspect {log_dir}')

    supervisor.run_turn = run_turn
    if args.dry_run:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = supervisor.main(argv)
        if code == 0:
            data = json.loads(output.getvalue())
            data['command'] = worker_command(data['command'], args.worker, args.effort, read_paths)
            print(json.dumps(data, indent=2))
        else:
            print(output.getvalue(), end='')
        return code
    return supervisor.main(argv)


if __name__ == '__main__':
    sys.exit(main())
