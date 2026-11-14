# Native monitoring usability

The macOS client keeps hardware ownership and archive writes in the core. This increment adds:

- Explicit local development-core launch and shutdown, with an interpreter picker for prepared Python environments. Personal RC keeps its existing supervised service.
- A graceful stop that finishes the active measurement, and a separate explicit force-stop action. The Swift client checks the advertised capability before requesting graceful stop from an older core.
- Separate core reachability and measurement freshness. A failed refresh retains the last spectrum visibly marked stale; receiver observations cannot make an old analyzer spectrum fresh.
- A full-width analyzer when no receiver is configured, smaller minimum window dimensions, and Russian primary controls and diagnostic explanations.

Raw measurement units, timestamps, archive ownership and generator policy are unchanged. tinySA generation is never enabled by these changes.

## Verification

Swift package tests cover stop negotiation, stale measurements and helper process lifecycle. Python tests cover independent worker shutdown, force escalation, cancellation and tinySA command-boundary recovery. These are synthetic checks, not hardware acceptance.

See [development core](macos-development-core.md) and [tinySA stop/restart](tinysa-stop-restart.md) for operational limits. Hardware results and full-suite results are reported separately in the pull request; private captures and local logs are not committed.
