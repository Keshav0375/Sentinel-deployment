# Scenarios — 30 branches, 3 cases

The app is real ground truth (deployment §4). Every scenario is a git branch on
`origin`, one `scenario:` commit on top of `main`, that changes `app/**` or
`requirements.txt`. Merged to `main`, it is built, deployed to App Service and watched by
Datadog like any other change. `branches.yaml` is the ground-truth label for each one,
and the eval runner (backend §13.2) scores Sentinel against it.

| Case | Branches | Pipeline | Live app | Datadog signal | `signal_type` |
|------|----------|----------|----------|----------------|---------------|
| i — clean pass | `pass/01..10` | green | healthy | success event only | `none` |
| ii — deploy fails | `deployfail/01..10` | red | build stage or an Oryx requirements failure: the previous version; the app cannot boot (deploy stage) or fails verify: **the broken version** | `sentinel-deploy-failure` monitor | `deploy_failure` |
| iii — runtime error | `runtime/01..10` | green | the broken version | a `sentinel-runtime-health` synthetic | `runtime_error` |

A deploy that leaves the app down or `/health` broken also trips a synthetic:
- `deployfail/07..10`: the app cannot boot, so `az webapp deploy` reports RuntimeFailed
  and the deploy stage fails with the broken version live.
- `deployfail/05`, which fails verify.

Those entries carry `also_expected: [runtime_error]`: one deploy, two alerts, and
Sentinel should correlate them to the same merge.

## Run a scenario

One at a time, against a live estate (infra applied, `grant-db-access.sh` run,
`datadog/apply.sh` applied).

1. **Open a PR** from the scenario branch to `main`, e.g.
   `gh pr create --base main --head runtime/01 --title "scenario: runtime/01"`.
   The deploy is push-only and the `sentinel-dev` environment is main-only, so nothing
   deploys until the merge.
2. **Merge it.** The push to `main` runs `ci_app_deployment.yml`, which deploys
   `pr-<N>-<sha>`. That version is what `expected_culprit: self` binds to.
3. **Observe** the Datadog signal, the bridge's `repository_dispatch` and Sentinel's
   incident (or, for `pass/*`, the absence of one).
4. **Revert the merge** through a PR ("Revert" on the merged PR, then merge the revert).
   The revert deploys the previous code. Wait for that run to go green, and for any
   runtime synthetic to recover, before the next scenario.

Never merge a scenario branch for any other reason: merging it deploys the fault.

## How long detection takes

- **Case ii:** the failed stage sends a `deploy_status:failed` event; the monitor
  evaluates a 5-minute window, so it alerts within about 5 min of the failed run.
- **Case iii, and `also_expected: [runtime_error]`:** the synthetics run every 30 min and
  retry a failure once a minute later, so an alert can take up to about **31 min** after
  the deploy. "Run test now" on the test in Datadog skips the wait.
- **`runtime/07`** degrades `/health` only once its package files are more than 10 min
  old (their mtime is the deploy). A manual "Run test now" must wait those 10 min, and a
  scheduled run inside them still passes, so detection can take up to about 41 min.
- **`runtime/04`** holds `GET /` for 90 s; the check times out at 60 s.

## Checks

`tests/test_scenarios.py` validates `branches.yaml` and checks every branch against it:
one commit on top of `main`, the paths it touches, a clean merge, and the fault itself,
run against the branch's own tree (verify's `/health` + `/version`, and the synthetics'
own assertions from `datadog/synthetics/`). The branch checks need the scenario refs
locally, as `origin/<branch>` or a local branch:

```bash
git fetch origin
.venv/bin/pytest tests/test_scenarios.py
```

A branch that is on origin but not fetched fails the check, and so does one missing from
origin. Only when origin cannot be reached does a local branch stand in. With no scenario
refs at all, those checks skip and say why, and the schema checks still run.
