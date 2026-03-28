# Operator Eval Examples

These files are tracked schema examples for the local-only `runtime/eval/` surface.
The suite example now illustrates both `trace-answer` and manual `ask-turn` replay cases.

The ask-turn example is intentionally synthetic:

- replay traffic is stamped `log_origin="evaluation-suite"`
- `required_run_events` should be authored in journal order
- the runner also performs shared-job closure checks from persisted runtime truth

They are intentionally insensitive and illustrative.
They exist so open-source users and agents can understand the required JSON shapes without any private corpus data or local benchmark history.

Do not treat these files as live runtime truth.
Live operator-eval artifacts belong under `runtime/eval/`.
