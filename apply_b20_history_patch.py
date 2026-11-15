#!/usr/bin/env python3
"""Idempotently integrate B20 history into existing V5 GitHub Actions workflow.
Does not run RPC, push, or deploy. Runs strict prerequisites BEFORE any write.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
WORKFLOW=ROOT/'.github/workflows/equity-evidence-pages.yml'
VALIDATE_PROXY=("      - name: Validate prior proxy evidence (offline, if available)\n"
                "        run: python scripts/proxy_monitor.py --check-only\n")
VALIDATE_B20=("      - name: Validate previous pinned B20 evidence (offline)\n"
              "        run: python scripts/b20_history.py --check-only\n")
BUILD_EVENT=("      - name: Build evidence-based change review (offline; no RPC)\n"
             "        run: python scripts/event_review.py\n")
BUILD_B20=("      - name: Collect native B20 Asset evidence pinned to same Base block\n"
           "        run: python scripts/b20_history.py\n")
ARTIFACT=("          cp data/alerts.json data/alerts_manifest.json _site/data/\n")
NEW_ARTIFACT=("          cp data/b20_latest.json data/b20_history.json data/b20_manifest.json _site/data/\n")


def transform(original):
    if not original.startswith('name: Equity Evidence - refresh and deploy\n'):
        raise ValueError('WORKFLOW_UNEXPECTED_NAME')
    if '  workflow_dispatch:' not in original or "    - cron: '17 3 * * *'" not in original:
        raise ValueError('WORKFLOW_EXPECTED_TRIGGERS_MISSING')
    if 'git add index.html .nojekyll data/' not in original or 'actions/deploy-pages@v4' not in original:
        raise ValueError('WORKFLOW_PUBLISH_CONTRACT_CHANGED')
    updated=original
    for anchor,new in ((VALIDATE_PROXY,VALIDATE_B20),(BUILD_EVENT,BUILD_B20),(ARTIFACT,NEW_ARTIFACT)):
        if new in updated:
            if updated.count(new)!=1:
                raise ValueError('DUPLICATE_B20_WORKFLOW_ANCHOR')
            continue
        if updated.count(anchor)!=1:
            raise ValueError('WORKFLOW_MISSING_ANCHOR')
        updated=updated.replace(anchor,anchor+new)
    return updated


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--check',action='store_true')
    args=p.parse_args(argv)
    try:
        if not WORKFLOW.is_file():
            raise ValueError('WORKFLOW_NOT_FOUND')
        for item in ('scripts/refresh_site.py','scripts/proxy_monitor.py','scripts/event_review.py',
                     'scripts/b20_native_probe.py','scripts/b20_history.py','tests/test_b20_history.py'):
            if not (ROOT/item).is_file():
                raise ValueError('REQUIRED_MODULE_MISSING: '+item)
        original=WORKFLOW.read_text(encoding='utf-8')
        new=transform(original)
        if args.check:
            print('B20_HISTORY_PATCH_COMPATIBLE_NO_WRITE')
        elif new==original:
            print('B20_HISTORY_PATCH_ALREADY_APPLIED')
        else:
            WORKFLOW.write_text(new,encoding='utf-8')
            print('B20_HISTORY_WORKFLOW_PATCH_APPLIED')
        print('No RPC, website, history, push, secrets or Pages deployment modified')
        return 0
    except (ValueError,OSError) as exc:
        print('B20_HISTORY_PATCH_STOPPED_NO_WRITE: '+str(exc),file=sys.stderr)
        return 2
if __name__=='__main__':
    raise SystemExit(main())
