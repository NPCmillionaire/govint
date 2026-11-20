# govint Landing Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Static one-page govint site on Cloudflare Pages.
**Architecture:** `site/index.html` + `site/style.css`, no build; deployed via wrangler direct upload.
**Tech Stack:** HTML, CSS, a few lines of inline JS, wrangler (via npx).
**Spec:** `docs/superpowers/specs/2026-11-19-landing-site-design.md`

## Global Constraints
- No framework, no build step; JS only for copy-to-clipboard.
- All source names, commands and install lines copied from README.md, not invented.
- Pages project name `govint`; deploy dir `site`.

## Review Focus
1. Phone width (375px): no horizontal page scroll; wide code/tables scroll inside their own box.
2. Copy button with clipboard unavailable (file://, old browser): page still usable, no JS error.
3. Sample dossier must be real `govint brief` output, trimmed, links intact.
4. Light-mode OS preference: page still readable (dark-only is fine if contrast holds).
5. Missing CSS (slow load): content order still makes sense as plain HTML.

---

### Task 1: Page and styles

**Files:** Create `site/index.html`, `site/style.css`, `site/_headers`

- [ ] Write index.html with sections: hero, sample, sources, commands, install, footer.
- [ ] Write style.css (dark, monospace accents, `overflow-x:auto` on pre/table wrappers).
- [ ] `_headers`: basic security headers + cache for style.css.
- [ ] Verify: open in browser at 1280px and 375px; `document.documentElement.scrollWidth <= innerWidth` at 375px; no console errors.
- [ ] Commit `site/` + docs.

### Task 2: Deploy

- [ ] `npx wrangler pages project create govint --production-branch master` (user runs `! npx wrangler login` first if needed).
- [ ] `npx wrangler pages deploy site --project-name govint --branch master`
- [ ] Verify: `curl -sI https://govint.pages.dev` returns 200 and the page loads in browser.
- [ ] Add a "Website" line to README pointing at the URL; commit and push.
