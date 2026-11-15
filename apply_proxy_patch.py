#!/usr/bin/env python3
"""Surgical, repeatable patch for the known Equity Evidence Lens V3 repository.
Does NOT touch generated site data or deploy to GitHub. Fails closed if source differs.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def plan(root=ROOT):
    src = root / 'engine/safe_rpc.py'
    wf = root / '.github/workflows/equity-evidence-pages.yml'
    if not src.is_file() or not wf.is_file():
        raise RuntimeError('V3_DEPENDENCIES_MISSING: expected engine/safe_rpc.py and .github/workflows/equity-evidence-pages.yml')
    rpc = src.read_text(encoding='utf-8')
    flow = wf.read_text(encoding='utf-8')
    if '"eth_getStorageAt"' in rpc or 'scripts/proxy_monitor.py' in flow:
        if ('"eth_getStorageAt"' in rpc and 'getStorageAt requires a valid address' in rpc
                and 'scripts/proxy_monitor.py' in flow
                and 'cp data/proxy_audit.json' in flow
                and 'python scripts/proxy_monitor.py --check-only' in flow):
            return {}  # Existing fully-installed V4 patch.
        raise RuntimeError('INCOMPLETE_PROXY_PATCH_FOUND: inspect before modifying')
        raise RuntimeError('PARTIAL_PROXY_PATCH_FOUND: inspect differences before modifying')
    method_old = '"eth_getBlockByNumber"})'
    method_new = '"eth_getBlockByNumber", "eth_getStorageAt"})'
    validator_anchor = '    elif method == "eth_getBlockByNumber":\n'
    validator_new = '''    elif method == "eth_getStorageAt":
        if not (isinstance(params, list) and len(params) == 3
                and isinstance(params[0], str) and ADDRESS_RE.fullmatch(params[0])
                and isinstance(params[1], str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", params[1])
                and isinstance(params[2], str) and BLOCK_TAG_RE.fullmatch(params[2])):
            raise ValueError("getStorageAt requires a valid address, exact bytes32 slot, and safe block tag")
''' + validator_anchor
    first = '      - name: Save complete history to default branch\n'
    second = '''      - name: Collect pinned proxy evidence and build review section
        run: python scripts/proxy_monitor.py
      - name: Save complete history to default branch
'''
    cp_old = '          cp data/earlier.json data/later.json data/comparison.json data/manifest.json data/history.json _site/data/\n'
    cp_new = ('          cp data/earlier.json data/later.json data/comparison.json data/manifest.json data/history.json _site/data/\n'
              '          cp data/proxy_audit.json data/proxy_history.json data/proxy_manifest.json _site/data/\n')
    before_old = '      - name: Get verified read-only Base block and build public history\n'
    before_new = ('      - name: Validate prior proxy evidence (offline, if available)\n'
                  '        run: python scripts/proxy_monitor.py --check-only\n'
                  '      - name: Get verified read-only Base block and build public history\n')
    for body, mark, name in ((rpc, method_old,'RPC_METHOD'),(rpc,validator_anchor,'RPC_VALIDATOR'),
                              (flow,first,'WORKFLOW_AUDIT'),(flow,cp_old,'WORKFLOW_ARTIFACT'),
                              (flow,before_old,'WORKFLOW_PRECHECK')):
        if body.count(mark) != 1:
            raise RuntimeError(f'UNRECOGNIZED_V3_LAYOUT: {name} marker count={body.count(mark)}')
    rpc = rpc.replace(method_old,method_new,1).replace(validator_anchor,validator_new,1)
    flow = flow.replace(first,second,1).replace(cp_old,cp_new,1).replace(before_old,before_new,1)
    return {src: rpc, wf: flow}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--check', action='store_true', help='No changes; verify compatible known V3 layout')
    args = p.parse_args(argv)
    try:
        changes = plan()
        if not args.check:
            old = {file: file.read_text(encoding='utf-8') for file in changes}
            completed = []
            try:
                for file, body in changes.items():
                    file.write_text(body, encoding='utf-8')
                    completed.append(file)
            except OSError:
                for file in reversed(completed):
                    file.write_text(old[file], encoding='utf-8')
                raise
        print('PROXY_PATCH_ALREADY_INSTALLED' if not changes else
              ('PROXY_PATCH_COMPATIBLE_NO_CHANGES' if args.check else 'PROXY_PATCH_APPLIED_TO_RPC_AND_WORKFLOW'))
        print('No public site, history, wallet, RPC or GitHub deployment modified')
        return 0
    except (RuntimeError, OSError) as ex:
        print(f'PROXY_PATCH_STOPPED: {ex}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
