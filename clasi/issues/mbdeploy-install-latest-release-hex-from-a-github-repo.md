---
status: pending
sprint: '002'
---

# mbdeploy: flash the latest release hex straight from a GitHub repo

## Description

Let `mbdeploy` take a **repo reference** instead of a hex file. It fetches
the newest release's hex asset and flashes it. For example:

```
mbdeploy deploy tovez --repo League-Microbit/nezha-robot-template
mbdeploy deploy zavaz --repo League-Robotics/microbit-radio-relay --force-relay
mbdeploy deploy tovez --repo League-Microbit/nezha-robot-template@v0.20260919.7
```

## Details learned from the real release pages

- **Assets.** Releases carry `MICROBIT.hex`, plus `MICROBIT.hex.txt` and
  sometimes a versioned copy (`nezha-robot-template-v0.20260919.7.hex`).
  Prefer `MICROBIT.hex`; otherwise take the single `*.hex`; error if the
  choice is ambiguous. `--asset NAME` overrides the choice.
- **Which release is newest.** `League-Robotics/microbit-radio-relay` has a
  **moving `latest` tag release** that is *not* GitHub's "Latest" release,
  alongside versioned `v0.YYYYMMDD.N` releases. Use GitHub's
  `releases/latest` API, the one flagged Latest, not a tag named `latest`.
  Support `@<tag>` to pin a release.
- **Fetching.** Use the public GitHub API anonymously, with an optional
  `GITHUB_TOKEN` for rate limits. Don't require `gh`.
- **Caching.** Cache downloads by `repo@tag`, e.g. under
  `~/.cache/mbtools/hex/`. Print which release and asset were flashed.
- **Remote flashing.** Download on the client, then send the bytes to the
  remote registry. The device host never needs internet access.
- **After flashing,** report the new announcement from the registry's
  post-flash re-probe, so the user sees the firmware changed.
