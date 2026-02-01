# Testing Branch

This branch is used for **testing and validation** before changes are merged into the mainline branch.

## What goes here
- Experimental changes
- Integration testing (multiple features together)
- CI/test verification
- Pre-release checks

## What should NOT go here
- Long-term work that isn’t ready for review
- Secrets / credentials (use `.env` and keep it out of git)

## Workflow
1. Create a feature branch from `testing`
2. Open a PR into `testing`
3. Validate tests/behavior
4. Open a PR from `testing` into `main` when stable
