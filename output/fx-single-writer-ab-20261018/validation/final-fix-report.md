# Final documentation cleanup

Commit: `6c7675722cda265bd205dced6ce0f939ae7ac769` — `docs: align writer and risk snapshot contracts`

Parent: `b41340805b398a7bc37034189108c6b2c8e69a6e`.

## Scope and self-review

Reviewed `final-review.md`, `task-4-review.md`, and the small relevant function/caller bodies before editing. Inspected the exact staged diff: six owned files only, 15 insertions and 10 deletions plus the rebuilt PDF. Production changes are exclusively comments/docstrings; no executable statements, financial behavior, queue behavior, API, tests, or dependencies changed.

- `credit/risk.py`: collateral valuation uses the fixed committed capture; missing required snapshots remain incomplete, without SQL filling or recapture.
- `fx/net_trading.py`: the target writer owns the transaction; execution locks Pair then User and captures final risk prices after the User lock.
- `api/v1/admin_fx.py`: cleanup runs in the pair writer after its Pair row lock; opening and repayment share that writer.
- `credit/execution.py`: target selection uses the User lock and fixed committed capture, with target execution serialized by its writer. The existing retry remains unchanged.
- `docs/latex/trading-system-guide.tex` and `.pdf`: expanded all five abbreviated FX kernel/liquidity paths to their actual `services/fx/` paths relative to `backend/app/`. No layout/style or completion/performance claims changed.

The deferred FX stop-during-recovery future timing concern remains deferred and untouched. No new recovery mechanism, feature, or validation project was introduced.

## Verification

All commands below exited 0:

```bash
# Worktree root, before commit
git diff --check
git diff --cached --check

# docs/latex, sequential passes
xelatex -interaction=nonstopmode -halt-on-error trading-system-guide.tex > /tmp/thccb-final-fix-xelatex-pass1.log
xelatex -interaction=nonstopmode -halt-on-error trading-system-guide.tex > /tmp/thccb-final-fix-xelatex-pass2.log
rg -n 'Overfull|Underfull|Missing character|undefined|Warning|Output written' /tmp/thccb-final-fix-xelatex-pass1.log
rg -n 'Overfull|Underfull|Missing character|undefined|Warning|Output written' /tmp/thccb-final-fix-xelatex-pass2.log
pdfinfo trading-system-guide.pdf
pdftotext -f 12 -l 12 -layout trading-system-guide.pdf /tmp/thccb-final-fix-guide-page12.txt
pdftoppm -f 12 -l 12 -scale-to 1500 -png -singlefile trading-system-guide.pdf /tmp/thccb-final-fix-guide-page12
```

Both build-log searches matched only `Output written on trading-system-guide.pdf (12 pages).`; no overflow, underflow, missing glyph, undefined-reference, or warning entries. `pdfinfo` confirms 12 A4 pages, PDF 1.5, 345237 bytes. Inspected the extracted final-page text and its direct PDF screenshot: full paths are readable, with no clipping or overlap. No browser was started.

An ephemeral `ast.parse` command parsed all four changed Python files successfully. No permanent tests were added; no unit/financial suite, PostgreSQL operation, service, load run, push, or deploy was performed. Existing functional evidence remains as qualified in `final-review.md`; this documentation-only wave does not strengthen or rerun it. Final controller review and fixed-ref A/B remain outstanding.

`git status --short` after the scoped commit was empty. This ignored handoff report is outside the commit, beside the final review.
