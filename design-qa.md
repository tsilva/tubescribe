# TubeScribe library design QA

Date: 10 October 2026. Selected target: the third displayed design, with a channel/video list and a summary reading pane. This is an update to the existing application and backend, rather than a separate mock-data prototype.

## Evidence and normalization

- Source visual truth: `/Users/tsilva/.codex/generated_images/01a1213d-c4a8-7f30-aebb-f2fa680812f3/exec-572a0fa7-7b3e-4068-a4a1-af0de401a2f9.png` (1487 × 1058 pixels).
- Implementation: `http://127.0.0.1:62173/`.
- Desktop CSS viewport: 1440 × 1024. Native browser screenshot: 1425 × 1013 pixels; the browser includes a page scrollbar and returns a slightly scaled capture. Both images were normalized to 1440 × 1024 for comparison; no content was reconstructed.
- State: unfiltered Jordi Visser library, second video selected, **A Bear Market Inside a Bull Market**, reader and page at the top. The real archive has 113 videos and a completed fourth video, unlike the illustrative four-row mock.
- Implementation screenshot: `data/quality/library-design-20261010/desktop-final.jpg`.
- Combined full-view evidence: `data/quality/library-design-20261010/comparison-final.jpg`.
- Combined focused evidence: `data/quality/library-design-20261010/comparison-list-focus.jpg` and `comparison-reader-focus.jpg`. These were inspected to judge row text/actions, selected state, reader typography, and linked figures.
- Mobile: 390 × 844 CSS pixels; `mobile-library.jpg` and `mobile-reader-final.jpg`.
- Intermediate desktop breakpoint: 1024 × 768, checked for horizontal overflow and reader visibility.

## Findings and comparison history

1. **[P2, fixed] Reading typography was too small.** Initial desktop capture used a smaller row and reader type scale than the selected target. Source headings, titles, and summary text were easier to scan. Increased the library heading to 44px, channel heading to 30px, row titles to 18px, reader title to 28px, and reader body to 16px; adjusted row/header spacing. The combined final full-view and focused comparisons show readable text and the intended hierarchy. The pre-fix capture is `desktop-before.jpg` and initial combined evidence is `comparison-before.jpg`.
2. **[P2, fixed] Mobile reader left the background page scrollable.** `mobile-reader-before.jpg` showed two scrollbars. Added body scroll locking while the mobile reader is open, background inertness, modal semantics, Escape handling, focus return, and keyboard focus wrapping. `mobile-reader-final.jpg` shows one reader scrollbar, the full viewport width, and accessible close/actions. Escape and both ends of the Tab loop were tested through the native browser.
3. **[P3, acceptable product differences] Real assets and source copy replace illustrative content.** Thumbnails/hero use saved transcript screenshots, preferring figures already linked by the summary. The mock’s stock photography and channel avatar were not invented or imported. The real summary retains its wording, emphasis, bullets, and figure links rather than substituting the mock’s short example text. It scrolls independently. Diagnostics are collapsed under each row, and SRT timestamps are an additional useful reading action. Empty/queued videos have text-only rows and explicit pending states. These are intentional production constraints, not missing placeholder assets.

## Required fidelity surfaces

- **Typography:** system sans-serif, strong library/channel/title hierarchy, 14–18px primary reading text by breakpoint, and smaller metadata. Focused reader comparison inspected.
- **Spacing/layout:** approximately 60/40 desktop library/reader split; horizontal app bar, search, channel grouping, divided rows, amber selected treatment, sticky independently scrolling reader. Four recent processed rows remain visible at the desktop target. Intermediate and mobile layouts have no horizontal overflow.
- **Colors/tokens:** white surface, dark ink, restrained gray metadata/dividers, pale amber selection and reading links, and green completion badges match the selected direction. Pending/failed states remain distinct and truthful.
- **Image quality:** real saved figures load through stable same-origin resource URLs. Reader figures retain their aspect ratio; compact thumbnails crop only for navigation. Missing images remove themselves rather than showing broken placeholders. Local, unmodified Heroicons v2.2.0 provide outline controls; license/source are bundled.
- **Copy/content:** all titles, dates, durations, profile states, and summary text come from the archive. No synthetic summary, profile, metric, or action is presented as live data. Worker liveness is retained.

## Interactions and verification

Native Codex in-app Browser checks:

- Title/thumbnail/Summary selection changes the reader to the correct real document.
- Full summary, Transcript, YouTube, and SRT links use the expected resources.
- Relative summary figure links resolve into the selected summary’s canonical directory.
- Search matches video IDs and titles; zero results shows an explicit empty state and preserves the current reading context.
- Channels collapse and matching searches expand them.
- Multiple profiles remain in one video row; diagnostics show both complete and unfinished states. Switching to the raw profile shows Awaiting diarization and a transcript excerpt.
- Refresh preserves the selected transcript profile.
- A queued video shows a pending state and source link without unavailable Summary/Transcript actions.
- Mobile selection opens the reader; closing/Escape restores focus to the selected title. Shift+Tab from close wraps to the last document link, and Tab returns to close.
- An isolated read-only server returned an unavailable document once; **Try again** made a fresh request and loaded the real summary. Unavailable HTML is not cached.
- The SRT page renders 655 cues for the selected video, with YouTube timestamps and download links.
- No horizontal overflow at 1440, 1024, or 390px widths. Reader images loaded. Browser console: no errors or warnings.

HTTP regression checks additionally cover publication/regeneration-safe cover URLs, actual image bytes, local icon delivery and path confinement, authenticated resource routes, and legacy downloads. JavaScript syntax, Python lint/format, and whitespace checks passed. The complete suite passed: **169 tests in 101.307 seconds**. The result is recorded in `data/quality/library-design-20261010/tests.log`.

## Implementation checklist

- [x] Implement the selected layout in existing dashboard assets.
- [x] Connect to existing read-only snapshot and published documents.
- [x] Preserve canonical routes, source/screenshot links, profile visibility, and diagnostics.
- [x] Correct typography and mobile modal scrolling/focus issues.
- [x] Compare source and final browser captures together, including focused regions.
- [x] Verify primary interactions, responsive layouts, and console output.

## Follow-up polish and limits

No actionable P0/P1/P2 findings remain. Stock photography and exact font-family matching are intentionally unnecessary for the real archive. Manual screen-reader testing and a wider operating-system/browser matrix were not performed. The source design did not specify an unavailable-document screen; the implementation provides loading, error, retry, and pending states. Failed document responses are not cached, so retry can make a fresh request.

final result: passed
