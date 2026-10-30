# TQQQ daily notification runtime overlay

This source backports daily notification behavior to the existing runtime
notification facade. It preserves the baseline dependencies and the existing
account display and candidate merging behavior. Pre-acknowledgement order
states remain urgent. Verify every source file against the selected immutable
image before applying this patch using `git apply --check --unidiff-zero` and
then `git apply --unidiff-zero`. This patch does not authorize a deployment or
change runtime settings.

Run notification, heartbeat, archive and receipt tests against the exact
baseline dependencies; verify image layers and configuration before rollout.
