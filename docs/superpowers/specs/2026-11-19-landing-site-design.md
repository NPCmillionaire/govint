# govint landing site — design

**Intent:** one static page that lets someone decide whether to install govint: what it does, what the output looks like, which sources it covers, how to install.

**Goals:** hero with `govint brief` example; real sample-dossier excerpt; sources table (from README, key column); one-line command list; install steps; GitHub link.
**Non-goals:** running searches in the browser, docs site, custom domain, git auto-deploy.
**Anti-goals:** a templated SaaS look (stock icons, gradients); content that drifts from what the CLI actually does.

**Constraints:** plain HTML + CSS in `site/` of the govint repo; no build step; JS only for copy buttons; works at phone width; deployed with `npx wrangler pages deploy site --project-name govint` to `govint.pages.dev`.

**Approaches:** (1) static HTML page — chosen; (2) static generator (Astro/Hugo) — overkill for one page; (3) Workers + D1 live search — deferred, much larger.

**Decided by user:** option 1 (static info site). **Builder's calls:** dark terminal/dossier aesthetic, exact copy, sample excerpt choice.
