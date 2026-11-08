# Source provenance and licensing

LOCKI AIRLOCK extends the LOCKED IN implementation by Dhruv Jain, Siddartha Kodithyala, Areeb Hirani, and Mustafa Hassan for HackUTD 2026. The preserved implementation originated in `Dhruvjain35/locked-in`, branch `milestone-2`, commit `59d3208`. Historical documentation is retained and identified in the root README; the AIRLOCK requirements and no-motor/no-flash restrictions supersede its older operating instructions.

The competition source snapshot contains the current reviewed source and documentation. It excludes private Git history, credentials, local runtime data, recordings, downloaded models, generated caches and installed dependencies. Public source availability does not establish completion of the physical, model or voice acceptance trials described in `docs/airlock/PRD_STATUS.md`.

The optional SunFounder Zeus Car firmware patch is **not distributed in this repository**; `firmware/` was excluded as recorded in [MIGRATION.md](docs/MIGRATION.md). In the original `Dhruvjain35/locked-in` source at commit `59d3208`, `firmware/README.md` and `firmware/optional/locked_in_patch.h` identify the patch as **GPL-3.0-or-later**. Those historical paths refer to the original repository, not this snapshot. This notice does not assign that license to unrelated repository files.

Dependency licenses remain those of their respective authors and are available through the pinned package manifests and installed distributions. No new blanket license is assigned to the rest of the team's source by this packaging notice.
