# Illustrated trading guide update

Commit: `4ce4081` — `docs: explain approved single-writer trading flow`

Owned changes only:

- `docs/latex/trading-system-guide.tex`
- `docs/latex/trading-system-guide.pdf` (12 A4 pages, 345261 bytes)

No Makefile, production code, dependencies, UI flow, or test changes. No push/deploy. Existing source and existing `touhouccb.sty` supplied the TikZ/vector style; no generated bitmap assets.

## Content

The guide now explicitly describes the approved 2026-10-18 target, with actual implementation, review, and final benchmark status delegated to the migration plan/SDD ledger. It does not claim final migration completion, passing final benchmark, or deployment.

Replaced obsolete GATE flow with per-market FX/LMSR writer plus independent immutable committed reads; retained FX DB execution authority and LMSR Decimal q. Documented release of authentication transactions/connections/locks before enqueue; execute-time foreign-market snapshots after acquiring User lock; fresh account balances/debts/holdings and economic version; no foreign-market freeze or nested writer waits; commit -> synchronous install -> account/market hint -> next command. Compound borrowing/trading remains one transaction. Included same user's 80/80 borrowing against 100 example with shared User lock and fresh ledger, explicitly distinct from account queues or admission policy.

Documented response-only SQL cold fallback and no overwrite/revival; active-owner bootstrap/unavailable behavior; queued lifecycle/register/reload/delete; financial/order/PvE boundaries; independent account/price version merging; ordinary/private SSE reads without market queues; exact-history public SSE retaining DB FOR SHARE and history coverage constraints. Historical performance table is marked entirely as old-baseline evidence, never as new writer performance. Source index is module-level to avoid guessing in-progress private function names.

## Verification

In `docs/latex`, final artifact built with these two sequential commands, both exit 0:

```bash
xelatex -interaction=nonstopmode -halt-on-error trading-system-guide.tex > /tmp/thccb-guide-xelatex-pass1.log
xelatex -interaction=nonstopmode -halt-on-error trading-system-guide.tex > /tmp/thccb-guide-xelatex-pass2.log
```

Inspected final pass log using `rg -n 'Overfull|Underfull|Missing character|undefined|Warning|Output written' /tmp/thccb-guide-xelatex-pass2.log`: only `Output written on trading-system-guide.pdf (12 pages).` matched. No overflow/underflow, missing glyph, undefined reference, or warning entries. `pdfinfo trading-system-guide.pdf` confirms 12 pages, A4, PDF 1.5; title/subject describe approved target.

Used `pdftotext -layout` to inspect extracted content/page references. Used direct `pdftoppm` PDF screenshots/contact sheet, reviewed all diagram-heavy pages and final corrected pages 3/7; labels do not overlap after moving the concurrent writer annotations. No browser automation or interaction tests. No permanent tests added. `git diff --cached --check` passed before the scoped commit. Commit contains only the two owned guide files.

## Final-controller fact check

No factual blocker in this approved-target guide. At final migration integration, compare the module index to any actual file moves and update completion/deployment/performance claims only when the controller has real final evidence. Current guide deliberately remains a target/progress-reference document while other tasks are pending.

## API protocol followup

Commit: `5978b3b` — `docs: clarify independent account and price snapshots`

Only `docs/api.md` changed (one protocol paragraph plus blank line at the private trading context/version/price section). It clarifies `minimum_group_stamps` as the floor of included prices, independently stable account versions and last-published market records, ordinary/private SSE initial prices without market writer wait, the exact-history/public FX SSE `FOR SHARE` exception, and execution risk snapshots after User lock plus freshly read personal ledger. Existing JSON, unrelated document text, and the lowercase FX business setting `gate开启` are unchanged. Edited the isolated branch's own file directly; did not copy the root workspace document or its unrelated one-line change.

Verification: `git diff --check -- docs/api.md` and `git diff --cached --check -- docs/api.md` both exit 0; reviewed scoped diff and committed only `docs/api.md`. No PDF rebuild, production tests, browser work, or additional dependencies, as requested. Final benchmark/deployment remain unclaimed. Guide commit remains `4ce4081`; this API-only followup is `5978b3b`.
