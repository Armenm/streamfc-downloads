# Tinify images in CI

This replaces the optional Ruby/fastlane pre-commit feature. No hooks are installed.
The workflow uploads eligible images to TinyPNG using the official Python SDK;
uploads consume the account's quota and share image contents with that external
service. No live compression has been verified: this environment has no API key.

## Setup

Merge these trusted files onto the default branch first. Set the repository
Actions secret **TINYPNG_API_KEY**. The runner needs Python 3.11+ and the labels
in the workflow (currently self-hosted, macOS, ARM64, iptv-backend). Hash-locked
dependencies are installed into an isolated temporary venv only after stdlib
preflight finds eligible images, and only when applying changes. PR dependencies,
configuration, ledger and executable code are never loaded from the PR head.

Use Actions → Tinify images → Run workflow on the default branch with a numeric
PR number. **dry_run defaults to true:** it validates selection, paths, blob
hashes and size limits without Pillow, SDK installation, uploads or writes.
Applying also validates image decoding, format and dimensions with Pillow.
An eligible, previously unseen image requires the secret; a PR with no eligible
images succeeds without that secret, SDK or Pillow.

Automatic runs accept only open, same-repository PRs authored by Armenm targeting
the default branch. The trusted checkout and PR base must match the current
default SHA; if the default branch advances, update the PR and retry. Bot senders
are skipped. Configured roots are public/assets/ and public/demo/ in config.json.
Only added/modified/renamed PNG, JPEG/JPG and WebP files qualify. Snapshots,
goldens, fixtures, screenshot/reference paths and nine-patch PNGs are excluded.
Symlinks, submodules and unsafe ancestors are rejected. Limits: 3,000 PR files,
30 images, 8 MiB each, 32 MiB total, 20 million pixels, 60 seconds per upload,
10 minutes per script invocation and a 15-minute workflow timeout. Animated,
malformed, resized or format-changed outputs fail the batch. The official SDK's
Requests response hook caps decoded response bytes before eager buffering;
redirects and non-Tinify origins are rejected, with a 20-second socket timeout.
Only smaller images are published; every result is prepared before any GitHub write.

## Signing, repeats and CI approval

Commits use the GitHub Git API with author, committer and signature omitted so
GitHub can sign the App commit. Before updating the branch the script reads the
created commit back and requires a valid verified signature, GitHub's committer
email and the GitHub Actions bot's exact author identity and email. If GitHub
cannot supply that authenticated identity/signature, the run fails and leaves
the branch untouched. This real signing path remains unverified without a key
and an authorized live run; local tests use API fixtures.

The SHA256 ledger is loaded from the trusted base plus signed, authenticated
marker commits with eligible compression-only changes. Their actual image
inputs/outputs and ledger are validated. Arbitrary PR ledger edits are ignored.
History is capped at 100 commits and ambiguous bot history fails safely.
The marker is reserved for this trusted workflow: do not let other workflows
execute untrusted code with a token capable of generating these signed,
compression-only commits. Git author strings or the message alone grant no trust.
Content hashes prevent repeated uploads across renames. Both accepted inputs
and outputs, including no-savings inputs, persist alongside actual compression
changes. If the whole batch has no savings there is no write, so those new
no-savings entries cannot persist and may be uploaded on the next run.

Publishing rechecks live PR state and fixed head/base, creates a child of the
fixed head, and updates the branch without force. API failures can leave orphan
objects; failures before ref update leave the branch untouched. Inspect the
branch if an update/readback fails; never force retry. Live rechecks detect
observed head/base changes; nonforce rejects divergent concurrent pushes. The
REST ref update is not an atomic expected-old-SHA compare-and-swap (an intentional
concurrent rewind cannot be distinguished after the final live check).

A published commit deliberately exits nonzero and prints **ACTION REQUIRED**
with the exact output SHA in the workflow summary. The known SHA and an uncertain-
publication warning are persisted before attempting the ref update, so even a lost
update response/readback retains the handoff. Rerunning an authenticated optimizer
HEAD repeats the nonzero handoff without uploading or writing, including stdlib
preflight when compression restored all image bytes to the base (zero candidates).
Manually approve the queued
existing normal CI run for **that exact SHA**, then review its required results.
If no run is queued, arrange normal CI for that exact SHA before merging.
GITHUB_TOKEN-created pull-request events can require workflow approval; push
events are suppressed. This is not unattended CI and the optimizer does not
claim that required checks passed. Existing ci.yml and required checks are unchanged.

## Verification and reuse

Credential-free tests (use a venv with requirements.lock installed):

    python -m unittest tools.tinify_ci.test_ci
    actionlint .github/workflows/tinify-images.yml

For each of the other seven repositories, review and copy tools/tinify_ci/ and
the workflow onto its trusted default branch, reset the ledger, customize
config.json asset roots and runner labels, review owner/signing assumptions,
set its own TINYPNG_API_KEY secret, and begin with manual dry runs. Apply to one
small owner-authored PR and verify signature and exact-SHA normal CI approval
before enabling automatic uploads. No other repository has been changed.
