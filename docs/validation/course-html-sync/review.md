# Five-module HTML sync review

The course in `docs/v2/course/` uses the reviewed five-module HTML from Workshop Studio commit `0044e8f842a87bf72de0706155c9f198133c11b3`, with the newer implementation contracts from this repository's upstream `25ea91f1f8926de32a6e8298384c65227f7a4d2a`. Each module presents one continuous sequence of decisions and activities and ends with one platform view.

The entry point remains [index.html](../../v2/course/index.html). The page set is Learning path, Introduction, Modules 1–5, Handoff and Clean up. The previous `module-N.html` and `module-N-rev.html` pages are superseded; their original content remains in Git history, including the latest seven-page revision at `25ea91f`.

The first sync at `e4c52ce` matched all 21 reviewed course, asset and worksheet files after adapting worksheet links to `templates/`. Fetching before publication found newer upstream changes. The final course preserves the reviewed narrative, topic anchors, decision figures and activity steps while carrying forward:

- Four approved model aliases, HR's Sonnet/GPT allow-list, Finance's fixed `nova-2-lite` model and the distinction between platform approval, project access and key scope.
- The `.workshop/state/` and `exports/` paths, plus the supplied record's `v1` prompt default.
- CLI self-reported evaluation versus worker-run evaluation from the Releases screen, including Dev deployment. Historical scorecard measurements retain their original model provenance and are not presented as a rehearsal of the current default.
- Cleanup's harness-interface wait, retry after `DELETE_FAILED` and current reference documents.

Eight files still match the reviewed source exactly after the original path adaptation. Six HTML pages have the changes above; seven SVG assets change only the model label. The existing style, JavaScript and three bundled worksheets are unchanged. README and both reference guides link to the new course and distinguish five learning modules from seven CLI build phases. Upstream's retired notebook and guide deletions are retained.

## Verification

| Check | Result |
| --- | --- |
| Current contracts and retained structure | Twelve checks pass against the model catalogue, HR record, fixed Finance project, CLI paths, evaluation and cleanup contracts, and all five modules' original topic anchors, figure counts and activity-step counts. |
| Source preservation | Eight unchanged files and thirteen explicitly adapted files; every SVG differs only by its model label. Per-file digests and adaptations are recorded in `validation.json`. |
| Desktop and mobile rendering | Nine pages at 1440px and 390px: 18 views, no page errors, broken assets, duplicate IDs or document overflow. |
| Local references | All 502 course references and 25 entry links resolve, including their HTML anchors. |
| Module reading journeys | 40 passing interactions across five modules at both widths: Contents, glossary, full-size platform image and next-page navigation. Preserved topic links and final platform placement also pass. |
| Guide entry | At both widths, the facilitator's Get started link reaches Introduction setup and the reference guide's learning-path link reaches the five-module course. Hero links retain readable contrast. |
| Visual inspection | The updated model-route figure and platform model labels fit their existing boxes. The mobile Module 2 opening retains the reviewed style and layout. |
| Scope | Implementation files match upstream `25ea91f`; the Workshop Studio checkout is unchanged. The user's `.gitignore` additions and Finder metadata were restored and verified; `.env.omni` metadata is unchanged and its contents were not read. |
| Whitespace | `git diff --check` passes. |

The Browser plugin was unavailable; verification used the installed Playwright Chromium runtime against [http://localhost:8083/](http://localhost:8083/). Initial checks caught the old page set and the missing upstream adaptations before they were corrected. The secondary facilitator guide has an existing 6px overflow at 390px; comparison with upstream confirms this update does not increase it. All nine course pages fit both widths. Results and validation notes are recorded in [validation.json](validation.json).

This is a documentation update on `docs/five-module-html-sync`. No AWS lab, application deployment or authenticated hosted acceptance was performed. The source explicitly leaves a fresh evaluation rehearsal with `claude-sonnet-5` pending.

## Review journey

1. Open the [learning path](../../v2/course/index.html). Expect five modules in the existing course style.
2. Open [Module 3](../../v2/course/m3.html). Expect HR and Finance to progress together through identity, tools and blueprints.
3. Open [Module 5](../../v2/course/m5.html), expand its glossary, and open its final platform image. Expect one concluding whole-platform view.
4. Open the [facilitator guide](../../facilitator-guide.html) and follow Get started. Expect the revised Introduction, current state path and working worksheet links.

Screenshots: [desktop module](module3-desktop.png), [mobile platform conclusion](platform-mobile.png) and [updated model route](model-route-desktop.png).
