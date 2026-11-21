# Hostile Git fixture inputs

These are text inputs, **not** repositories. Tests create the `.git` directory,
executable hook/filter scripts, local Git configuration, and marker paths in
`tmp_path` for each case. Never commit a nested `.git` directory or execute
these instructions. The `.invalid` URL is deliberately non-routable; no test
performs a network operation.
