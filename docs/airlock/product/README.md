# Airlock product research and scenario coverage

Prepared 8 November 2026. This package examines whether Airlock can protect a shared engineering workstation without disrupting ordinary colleagues or the owner's work. It contains research and planned acceptance work, not executed physical tests or implemented fixes.

- [Product assessment and sourced findings](product-research.md): actual product scope, important failure modes, existing OS controls and the value hypothesis.
- [108 detailed scenarios](scenario-register.md) ([CSV](scenario-register.csv)): proposed behavior, priority and source evidence or gap. Every scenario remains planned and unexecuted in this package.
- [1,000 candidate combinations](coverage-grid-1000.csv): authorization, activity, workstation state and coverage conditions. These require applicability review and exact acceptance criteria; they are not 1,000 passed tests, discovered bugs or exhaustive coverage.

## Reading the evidence

Source review establishes a software boundary; an automated fixture establishes only that fixture's boundary. Neither proves a camera's physical accuracy, acoustic warning, real screen lock, workload compatibility or end-to-end deployment. Record those results separately and retain failures and missing data.

The [release acceptance record](../RELEASE_ACCEPTANCE.md) and [accuracy protocol](../ACCURACY_TRIALS.md) remain the integration and measurement references. Implementation is moving: the release record in publication snapshot `1bbbfbf` contains historical statements that Mustafa's service is absent, while `services/nemotron-investigator/` now exists. Imported source does not establish deployment or live model acceptance. Read each statement with its snapshot and evidence level.

The useful product hypothesis is faster, factual review of meaningful workstation changes, with narrowly authorized responses. A camera observation cannot determine friendship, permission or malicious intent. Ordinary OS lock and accessory controls remain the access-protection baseline.

Raw camera/audio recordings, private identities, tokens and incident payloads must stay out of git under [CONTRIBUTING.md](../../../CONTRIBUTING.md). Publish sanitized result summaries only.
