# Five-module HTML sync implementation plan

> **For agentic workers:** Use the executing-plans workflow to complete these steps in order.

**Goal:** Update this repository's HTML course to the reviewed five-module version already committed in the Workshop Studio repository.

**Architecture:** Keep `docs/v2/course/index.html` as the entry point and install the nine reviewed pages from Workshop Studio commit `0044e8f`. Bundle the three linked worksheets inside the course folder so the HTML is self-contained. The previous seven-module pages remain available through Git history.

**Tech stack:** Static HTML, CSS, JavaScript and SVG; Python standard library for copying and structural checks; the installed Playwright runtime for browser verification.

**Spec:** The user's request to update the HTML in this repository while leaving the current Workshop Studio copy in place, including the accepted continuous module narratives and one concluding platform view per module.

## Constraints

- Work on `docs/five-module-html-sync`.
- Preserve existing changes to `.gitignore`, `docs/.DS_Store` and `.env.omni`; do not read credentials.
- Keep the reviewed five-module narrative, decision tables and activities. Adapt worksheet links and carry forward the newer upstream runtime contracts listed below.
- Keep the CLI's seven build phases and design-record keys unchanged.
- Do not change implementation code, AWS resources or the Workshop Studio checkout.

## Sync and validate the course

**Files:** `docs/v2/course/`, the course entry links in `README.md`, `docs/facilitator-runbook.md` and `docs/facilitator-guide.html`, and sanitized evidence under `docs/validation/course-html-sync/`.

- [x] Check the destination against the expected nine-page set and reviewed source bytes; record the missing/outdated pages before copying.
- [x] Copy the nine HTML pages, CSS, JavaScript and seven SVG assets from commit `0044e8f`. Copy `journey-record.md`, `component-decision.md` and `evidence-checklist.md` to `docs/v2/course/templates/`; replace `../../static/templates/` with `templates/` in HTML links.
- [x] Remove the fourteen superseded `module-1.html` through `module-7.html` and `module-1-rev.html` through `module-7-rev.html` pages. Update the README and facilitator setup links to `intro.html#setup-workspace`.
- [x] Re-run content parity and local reference checks. Expect nine course pages, exact content parity after the declared worksheet-link adaptation, and no missing local files or anchors.
- [x] Serve `docs/v2/course/` locally and check all nine pages at 1440px and 390px. Test Contents, the module glossary, topic links, final platform images and the next-page pager. Inspect representative desktop and mobile screenshots.
- [x] Confirm the Workshop Studio checkout and unrelated files are unchanged, record the results, run `git diff --check`, and commit only this course update on the feature branch.

## Integrate the newer upstream course contracts

Fetching before publication found upstream `25ea91f`, newer than the original `d16db31` baseline. Merge its implementation unchanged and retain the five-module course. The initial exact source-parity result belongs to commit `e4c52ce`; the final course needs the adaptations below.

**Files:** The nine course pages and their diagrams, `README.md`, `docs/facilitator-guide.html`, `docs/workshop-guide.html`, this plan and the validation evidence. Honor upstream's deletion of `docs/facilitator-runbook.md`, the notebook labs and retired documentation.

- [x] Carry forward the four approved model aliases, HR's two-model allow-list, Finance's `nova-2-lite` model, `.workshop/state/` and `exports/` paths.
- [x] Explain CLI self-reported scores versus platform-run Releases evaluation, preserve historical measurement provenance, and update the production backlog.
- [x] Carry forward the cleanup command's harness-interface wait, retry and current reference documents.
- [x] Resolve entry links and distinguish five learning modules from seven CLI build phases in the secondary guides.
- [x] Verify the current contracts against upstream source, repeat the nine-page browser checks and module journeys, inspect changed diagrams, and refresh evidence.
- [x] Complete the merge, restore and verify the user's autostashed edits, and confirm implementation files match upstream.

**Publication:** The user has authorized pushing workshop work and merging to `main`. Publish after the checks above, verify the remote revision, and report it in the handoff.
