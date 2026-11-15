#!/usr/bin/env python3
"""Fail-closed, idempotent patch to the known V4 GitHub Actions workflow.
Never changes histories, deployed site content, network settings or secrets.
"""
from pathlib import Path
import argparse
import sys

ROOT=Path(__file__).resolve().parent
WORKFLOW='.github/workflows/equity-evidence-pages.yml'


def plan(root=ROOT):
    path=root/WORKFLOW
    if not path.is_file() or not (root/'scripts/proxy_monitor.py').is_file() or not (root/'scripts/refresh_site.py').is_file():
        raise RuntimeError('EXPECTED_V4_REPOSITORY_NOT_FOUND')
    body=path.read_text(encoding='utf-8')
    if 'scripts/event_review.py' in body:
        required=('python scripts/event_review.py --check-only',
                  'run: python scripts/event_review.py\n',
                  'cp data/alerts.json data/alerts_manifest.json _site/data/')
        if all(x in body for x in required): return None
        raise RuntimeError('INCOMPLETE_V5_WORKFLOW_PATCH')
    markers={
      'precheck':'      - name: Validate prior proxy evidence (offline, if available)\n',
      'post_proxy':'      - name: Save complete history to default branch\n',
      'artifact':'          cp data/proxy_audit.json data/proxy_history.json data/proxy_manifest.json _site/data/\n',
      'proxy_run':'        run: python scripts/proxy_monitor.py\n'
    }
    if any(body.count(s)!=1 for s in markers.values()):
        raise RuntimeError('UNRECOGNIZED_WORKFLOW_LAYOUT_DO_NOT_OVERRIDE')
    body=body.replace(markers['precheck'],
        '      - name: Validate prior event review (offline, if available)\n'
        '        run: python scripts/event_review.py --check-only\n'+markers['precheck'],1)
    body=body.replace(markers['post_proxy'],
        '      - name: Build evidence-based change review (offline; no RPC)\n'
        '        run: python scripts/event_review.py\n'+markers['post_proxy'],1)
    body=body.replace(markers['artifact'],markers['artifact']+
        '          cp data/alerts.json data/alerts_manifest.json _site/data/\n',1)
    return body


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');args=p.parse_args(argv)
    try:
        contents=plan(ROOT)
        if contents is not None and not args.check:
            (ROOT/WORKFLOW).write_text(contents,encoding='utf-8')
        print('ALERT_PATCH_ALREADY_INSTALLED' if contents is None else
              'ALERT_PATCH_COMPATIBLE_NO_WRITE' if args.check else 'ALERT_PATCH_WORKFLOW_UPDATED')
        print('Historical data, public HTML, trading, wallet and secrets unchanged.')
        return 0
    except (RuntimeError,OSError) as ex:
        print(f'ALERT_PATCH_STOPPED: {ex}',file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
