# Publishing REXLiTE for HACS

This repository publishes the REXLiTE custom integration for Home Assistant through HACS.

## Release checklist

1. Keep the integration version and `CHANGELOG.md` synchronized.
2. Run `./scripts/verify.sh` and confirm all GitHub Actions pass.
3. Test installation, setup, restart, update, and removal on supported Home Assistant environments.
4. Confirm recovery behavior after a temporary service interruption.
5. Confirm public logs and support data contain no customer or credential information.
6. Create a version tag and a complete GitHub Release.
7. Install the released version through HACS on a clean system.

Before publishing, confirm the repository metadata, validation workflow, release notes, and public artwork are complete.
