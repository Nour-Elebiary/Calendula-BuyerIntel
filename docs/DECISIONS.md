# Decisions Log

Project: Calendula BuyerIntel | Owner: Nour Eldin

Every override of the spec or an addendum entry is recorded here with the date and the
reason, per the instruction in `docs/01_SPEC.md` section 0.

| Date | Area | Decision | Reason |
| --- | --- | --- | --- |
| 2026-11-14 | Repo | Initialized repository at `D:\Sales-Ultimate-Calendula` with the layout from spec section 4 (`src/buyerintel/...`, `config/`, `tests/`, `docs/`). The three user documents were moved from the root into `docs/` under the canonical names `01_SPEC.md`, `02_WHAT_THE_USER_TOLD_ME.md`, `03_ADDITIONS.md`. | The spec's own reading instructions reference these `docs/` paths; the root-only layout predated the scaffold. |
| 2026-11-14 | Toolchain | Target Python is 3.12 via `uv` (spec section 3). The system Python on this machine is 3.14; `uv` will install and pin 3.12 for the project venv. | Spec compliance; `uv` manages interpreter versions itself. |
| 2026-11-14 | GitHub | Remote: private GitHub repository `Nour-Elebiary/Calendula-BuyerIntel` (private per the owner's instruction). | Owner requested a private remote kept in sync. |
