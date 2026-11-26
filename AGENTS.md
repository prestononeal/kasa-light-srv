# Pull requests

When asked to open a PR, commit on a feature branch and push with `git push -u origin HEAD`.
Use `gh pr create` with explicit `--base`, `--head`, `--title`, and `--body-file` arguments.
Verify the PR with `gh pr view` and return its URL. If `gh` is missing or unauthenticated,
report the blocker. Use the browser only if explicitly requested.
