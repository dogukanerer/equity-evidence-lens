# Equity Evidence Lens — B20 Native Evidence V6

**Date:** 2026-11-16 · **Type:** additive, fail-closed patch · **No wallet/private key/transaction**

## Why this is necessary

Base B20 Asset tokens implement their logic as native precompiles, so classic ERC-1967/1167 checks alone do not describe their authority model. Two supply readings matter:

- `totalSupply()` is **raw token units**, as used by onchain balance and transfer accounting.
- `totalSupplyUI()` is **UI-adjusted token units**, using `uiMultiplier()` (18-decimal WAD) and integer rounding down.

The user's **live read-only pinned snapshot** at block **57,958,688** reported NVDAc raw `49255.60410884`, UI-adjusted `49282.10064764`, multiplier `1.000537939576369481`. AAPLc and TSLAc had multiplier 1. These values are a **historical observation**, not a claim about the latest block. There were no live pending updates at that block; this is not a promise of future stability.

Primary technical references:
- https://github.com/base/base-std/blob/main/docs/guides/scheduling-stock-splits.md
- https://github.com/base/base-std/blob/main/changelog/02_Cobalt_B20Asset_multiplier.md
- https://www.base.org/blog/introducing-base-beryl

## What this patch adds

1. `scripts/b20_history.py` validates the existing Base public snapshot history with the V3 validation code and determines its **latest pinned block**. It does NOT choose a different block.
2. Same-block native B20 reads for `totalSupply`, `decimals`, `uiMultiplier`, `newUIMultiplier`, `effectiveAt`, and `totalSupplyUI` from exactly three allowlisted contracts. RPC block number/hash and timestamp are checked before and after reads.
3. On-chain raw total supply and `decimals` must **equal the values in the existing verified core record** at the same block. `totalSupplyUI == floor(totalSupply * uiMultiplier / 10^18)` must hold. Any disagreement aborts the workflow.
4. JSON evidence: `data/b20_latest.json`, `data/b20_history.json`, `data/b20_manifest.json` and an HTML section called **B20 yerel varlık kanıtları**. Prior B20 history remains immutable and samples are tied to core Base blocks.
5. The GitHub workflow is only modified **after** compatibility checks. The V3/V4/V5 site build steps are preserved. A failure in B20 data collection causes the run to fail *before Git commit or Pages deployment*; the previously published site remains available, but that day's refresh may not publish.

We **do not backfill invented multiplier values** for historical core samples. B20 history begins with the first successful V6 workflow. Initial evidence is a baseline, not a "new multiplier change". Once a second B20 observation arrives, sampled changes can be compared.

## Installation (on your Mac)

Before installing the ZIP, update from GitHub after the existing Actions jobs have finished:

```bash
cd ~/Desktop/equity-evidence-lens-site
git status --short
git pull --ff-only
```

Some untracked files from the earlier B20 one-time probe (`B20_ONCE_OKU.md`, `SHA256_B20_PATCH.txt`, `scripts/b20_native_probe.py`, `tests/test_b20_native_probe.py`) are expected. **Do not remove or overwrite them.** If Git reports conflicts, stop instead of forcing anything.

Download the ZIP into `~/Downloads`, then:

```bash
cd ~/Downloads
unzip -n Equity_Lens_B20_History_V6_2026-11-16.zip -d ~/Desktop/equity-evidence-lens-site
cd ~/Desktop/equity-evidence-lens-site
python3 apply_b20_history_patch.py --check
python3 apply_b20_history_patch.py
python3 -m unittest discover -s tests -q
python3 scripts/b20_history.py --check-only
```

Expected: `B20_HISTORY_PATCH_COMPATIBLE_NO_WRITE`, `B20_HISTORY_WORKFLOW_PATCH_APPLIED`, **`Ran 164 tests` and `OK`**, and `B20_LOCAL_CHECK_NO_NETWORK` with `written: false`. The existing site and history are unchanged by these commands.

Only when the above passes:

```bash
git add .github/workflows/equity-evidence-pages.yml \
  scripts/b20_native_probe.py scripts/b20_history.py \
  tests/test_b20_native_probe.py tests/test_b20_history.py \
  apply_b20_history_patch.py B20_V6_KURULUM_VE_SINIRLAR.md \
  SHA256_B20_HISTORY_PATCH.txt
git diff --cached --stat
git commit -m "Add pinned B20 multiplier and UI supply evidence"
git push origin main
```

Open **GitHub Actions**: https://github.com/dogukanerer/equity-evidence-lens/actions . Run **Equity Evidence - refresh and deploy** with branch `main` *once*, after confirming that no other run is active. Check that `refresh` and `deploy` are both successful. The refresh stage has two additional steps: prior B20 validation and pinned B20 collection.

After publication: https://dogukanerer.github.io/equity-evidence-lens/#b20-native-evidence-v1 . The three new JSON links must load. Git pushes alone do not initiate a workflow because this repo has `workflow_dispatch` and `schedule`, not `push`.

## Interpretation and limitations

- A multiplier above 1 is **not by itself evidence of a newly occurred corporate action**, fraud, profit, or an additional issue of stock or tokens.
- A lack of a pending multiplier schedule says **only** that none was pending at that sampled block. It does not exclude future changes or an emergency setter.
- B20 authority roles, transfer and pause policies, custody, actual shareholder rights, issuer approvals, price, tax, liquidity and airdrop eligibility are **not** audited by V6.
- The `totalSupplyUI` method returns a multiplier-adjusted token-display figure; it is not necessarily the number of real shares held in custody.
- RPC observations are not cryptographic proofs; there can be changes and reversals between sampled blocks. The 32-block lag is not a finality guarantee.
- Existing V4 classic proxy diagnostics are retained for transparency, but should not be described as the B20 authority audit.

## Rollback if a new workflow fails

Do not `git push --force`. Review the failing GitHub Actions step. The previously successful Pages site stays online. If necessary revert **only the commit that added this patch**, using a regular `git revert <actual-commit-hash>` after inspecting subsequent bot-generated data commits. Avoid blindly reverting the entire repository or removing history JSON.
