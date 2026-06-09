# Plan: Agent-Friendly GitHub Hardening (F1–F6, F11, F12)

Profile: **solo — interim** (one human maintainer, no distinct agent identity; reviews 0).
Repo: public, traditional, default branch `main`. All work on branch `chore/agent-friendly-hardening`.

Two change classes:
- **Files** (land via PR/commit on branch): F2, F3 (dependabot.yml), F4, F5, F11, plus a new CI aggregate gate job.
- **Live repo settings** (via `gh api`, not in branch; applied AFTER files land): F1 ruleset, F3 (enable alerts/security updates), F6 (CodeQL), F12 (labels). Each confirmed with user before applying since they are outward-facing config.

## Sequencing (matters)
1. Commit file changes on branch → open PR → merge to `main` (solo: green PR self-merged).
   - Includes the new `ci-success` aggregate job so a stable required-check context exists on `main`.
2. Verify `ci-success` check appears on a `main` run.
3. Create F1 ruleset requiring `ci-success` (creating it earlier would require a context that doesn't exist yet → permanent pending).
4. Apply F3 enablement, F6 CodeQL, F12 labels (order-independent).

---

## F1 — Protected-branch ruleset on `main` (solo interim)
Create via `POST /repos/briandconnelly/cwms-tools/rulesets`:
- `target: branch`, conditions ref_name include `~DEFAULT_BRANCH`.
- `enforcement: active`.
- `bypass_actors: []` (empty — interim; maintainer merges green PRs with reviews 0, no bypass needed).
- Rules:
  - `pull_request` with `required_approving_review_count: 0`, `dismiss_stale_reviews_on_push: true`, `require_code_owner_review: false`, `require_last_push_approval: false`, `required_review_thread_resolution: false` (Codex: rulesets API marks this required within `pull_request` params — include it explicitly).
  - `required_status_checks`: strict (`strict_required_status_checks_policy: true`), required check = **`ci-success`** (single aggregate context; integration_id omitted/GitHub Actions).
  - `required_linear_history`.
  - `non_fast_forward` (block force-push).
  - `deletion` (block branch deletion).
- NOT included (N/A solo interim): merge queue, required_signatures, required_deployments.

New CI job in `.github/workflows/ci.yml` (so a stable required context exists; matrix names are dynamic and tag-only jobs must never be required):
```yaml
  ci-success:
    name: CI success
    needs: [test, build]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: Gate on required jobs
        env:
          TEST_RESULT: ${{ needs.test.result }}
          BUILD_RESULT: ${{ needs.build.result }}
        run: |
          if [ "$TEST_RESULT" != "success" ] || [ "$BUILD_RESULT" != "success" ]; then
            echo "required jobs failed: test=$TEST_RESULT build=$BUILD_RESULT"; exit 1
          fi
```
(Required context name will be `CI success` — confirm exact string GitHub records before wiring the ruleset.)

## F2 — Pin all third-party actions to full commit SHAs
For each `uses:` in ci.yml + mcpb.yml, resolve the tag → 40-char SHA and pin with the version in a trailing comment. Targets:
- `actions/checkout@v6`
- `astral-sh/setup-uv@v8.1.0`
- `actions/setup-node@v6`
- `actions/upload-artifact@v7`
- `actions/download-artifact@v8`
- `softprops/action-gh-release@v2`
- `pypa/gh-action-pypi-publish@release/v1` (branch ref → pin to the SHA the current release/v1 points to; highest priority, it publishes to PyPI)
Local `uses: ./.github/workflows/mcpb.yml` is first-party — leave as path.
Resolve SHAs via `gh api repos/<owner>/<repo>/commits/<tag>` (or the tags/refs API) at implementation time.

## F3 — Dependabot
- Enable alerts: `PUT /repos/.../vulnerability-alerts`. Enable security updates: `PUT /repos/.../automated-security-fixes`.
- Add `.github/dependabot.yml`:
  - `github-actions` ecosystem (dir `/`, weekly) — automates future F2 SHA bumps.
  - Python ecosystem for pyproject/uv.lock. **OPEN QUESTION for Codex:** correct `package-ecosystem` value for a uv project — `uv` (native, newer) vs `pip`. Verify which Dependabot currently honors for `uv.lock`.
- Dependabot PRs flow through the same `ci-success` required check (F1). No auto-merge configured.

## F4 — Canonical AGENTS.md + thin CLAUDE.md
- Create root `AGENTS.md` as the single source of truth. Fold in current `CLAUDE.md` content and expand with operate norms the audit found missing:
  - Tooling: uv, ruff (format+lint), ty, prek, FastMCP 3, Typer; test = `uv run pytest`; coverage target 95%+.
  - Branch naming: `feat/`, `fix/`, `chore/`, `docs/`, `ci/`, `release/` (matches existing history).
  - Commit format: conventional commits (matches history).
  - PR/review expectations; CHANGELOG entry required; tag↔pyproject↔CHANGELOG must agree (already CI-enforced).
  - Release flow: tag on merge to main → CI publishes.
  - Keep the `@cwms-overview.md` doc pointer.
- Replace `CLAUDE.md` body with a single line: `@AGENTS.md`.
- Mirror question for Codex: `.agents/` tree also exists — confirm we don't need a parallel pointer there (it holds skills, not instructions).

## F5 — .gitattributes
Add at repo root:
```
* text=auto eol=lf
```
- Minimal; do NOT mark `uv.lock` as generated (lockfile diffs are part of dependency review).
- Note: first commit may produce a one-time line-ending normalization. Verify tests/.cassettes (recorded fixtures) are text and tolerate normalization; if any cassette is byte-sensitive, add `tests/.cassettes/** -text` to leave them untouched. **Flag for Codex: cassette normalization risk.**

## F6 — CodeQL (default setup)
- Enable via `PUT /repos/.../code-scanning/default-setup` with `state: configured`, `languages: [python]` (free on public repo). Confirm default-setup API is acceptable vs an advanced-setup workflow file. Default setup preferred (no required-check entanglement; not added to F1's required checks).

## F11 — Dependency review on PRs
- Add a `dependency-review` job to ci.yml triggered on `pull_request` only:
```yaml
  dependency-review:
    name: Dependency review
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>  # v6
      - uses: actions/dependency-review-action@<sha>  # vX
```
- NOT added to F1 required checks (only runs on PRs; would be fine, but keep the single `ci-success` gate as the required context). Optionally fold into `ci-success` needs — decide with Codex.

## F12 — Priority labels
Create via `gh label create`:
- `priority/high` (color e.g. b60205)
- `priority/medium` (fbca04)
- `priority/low` (0e8a16)
Type labels already exist. No `scope/*` (not a monorepo).

---

## Verification
- `prek run --all-files` / ruff / ty clean on the branch.
- After ruleset: confirm `gh api .../rulesets` shows the rule; attempt nothing destructive.
- After F3/F6: `gh api .../vulnerability-alerts` 204; `security_and_analysis.dependabot_security_updates: enabled`; code-scanning default-setup state.
- Update CHANGELOG with an entry for the hardening.

## Codex review outcomes (resolved)
1. Dependabot ecosystem = **`uv`** (native; uv.lock supported). `pip` workaround is stale.
2. `ci-success` gate (`if: always()` + `needs:[test,build]`, check `needs.*.result`) is correct — a failing matrix leg makes `needs.test.result` non-success and fails the gate. No job-level `continue-on-error` on test/build (the `ty` `continue-on-error` is step-level, intentional, non-blocking).
3. **Critical sequencing:** the required-check context name must match the job's check name string EXACTLY, and `ci-success` must have reported once on `main` before it is enforced — otherwise it stays permanently pending and blocks all merges. Confirm exact string in the UI/API after the job runs, then enforce.
4. SHA-pinning `pypa/gh-action-pypi-publish` is safe — PyPI trusted-publishing OIDC matches on owner/repo/workflow-filename/environment, NOT the action ref. Keep `github-actions` Dependabot to refresh pins.
5. Empty bypass + reviews 0 does NOT lock out the lone maintainer; the only real lockout risk is a never-reporting required check.

## Remaining open items (verify at implementation)
- `.gitattributes` normalization risk for `tests/.cassettes/**` — inspect a cassette; add `tests/.cassettes/** -text` if byte-sensitive.
- CodeQL: prefer default-setup API; confirm acceptable vs advanced workflow.
- Whether to fold `dependency-review` into `ci-success` needs (decision: keep separate; not required).

## Original open questions (now answered above)
1. Dependabot `package-ecosystem` for uv (`uv` vs `pip`)?
2. `.gitattributes` normalization risk for `tests/.cassettes/**` — exclude from text normalization?
3. CodeQL default-setup API vs advanced workflow — any reason to prefer advanced here?
4. Should `dependency-review` and/or the matrix be folded into `ci-success` needs, or is gating on `[test, build]` sufficient?
5. Any ordering hazard in creating the ruleset's required `ci-success` context (does GitHub accept an arbitrary context string via API before it has ever reported)?
