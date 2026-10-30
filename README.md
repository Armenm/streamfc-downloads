# StreamFC downloads

Sideload builds for Fire TV / Android TV. Install guide: https://streamfc.app/fire

Latest APK: https://github.com/Armenm/streamfc-downloads/releases/latest/download/streamfc.apk


## Trusted TinyPNG CI

The previous local Ruby/Fastlane hook proposal is replaced by [official-SDK CI](tools/tinify_ci/README.md); no local hook is installed. Trusted production roots: `public/assets/`. No currently tracked eligible images exist. public/assets/ is reserved for future production documentation/assets only; test screenshots, baselines, fixtures and goldens remain excluded. No assets are added by this PR.

Repository runner: `[self-hosted, macOS, ARM64, streamfc-downloads]` with Python 3.11+. The reusable linked README retains upstream backend root/runner examples; this section, `config.json` and the workflow are authoritative for this repository. Default-branch activation requires a separately authorized merge; this PR does not activate the workflow. Add `TINYPNG_API_KEY` as an Actions secret only when ready for external uploads/quota use. No real upload, signed bot publication, or end-to-end approval is verified without a key.

After bot publication, a human must approve or arrange normal tests for the **exact bot SHA**, including the no-queued-run fallback; normal PR Actions are not currently configured here. Existing deployment/test commands are unchanged. All-no-savings batches cannot persist new ledger entries and may reupload next run. Nonforce GitHub ref updates are not atomic expected-old-SHA CAS.
