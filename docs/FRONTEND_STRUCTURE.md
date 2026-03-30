# Frontend Structure Guide

This project now uses a page-scoped frontend structure for Flask templates, CSS, and JavaScript.

## Folder Layout

- `dashboard/templates/base.html`
  - Shared shell (header, nav, warmup banner/overlay, global shared scripts).
- `dashboard/templates/pages/`
  - One template per page (`dashboard.html`, `model.html`, `experiment.html`, `respondents.html`, `sessions.html`).
- `dashboard/static/css/core/`
  - Global tokens and reusable base components.
- `dashboard/static/css/layouts/`
  - Reusable page layout patterns (for example fixed dashboard layout).
- `dashboard/static/css/pages/`
  - Page-specific styles.
- `dashboard/static/css/shared/`
  - Shared cross-page UI modules (for example warmup runtime UI).
- `dashboard/static/js/pages/`
  - Page-specific behavior.
- `dashboard/static/js/shared/`
  - Shared cross-page client utilities.

## Shared JS Utilities

- `sse_client.js`
  - Adds resilient SSE reconnection with backoff and optional JSON parsing.
  - Supports hidden-tab pause/reconnect via `pauseWhenHidden: true`.
- `chart_utils.js`
  - Centralized chart resize hardening using debounce + requestAnimationFrame.
  - Uses `ResizeObserver` when available.
- `warmup_ui.js`
  - Normalizes runtime state (`INIT`, `WARMUP`, `WAITING_SENSOR`, `RUNNING`, `DEGRADED`).
  - Updates warmup banner, runtime pill, and optional overlay.

## Implementation Rules

- Keep shared logic in `static/js/shared/`.
- Keep per-page logic in `static/js/pages/`.
- Avoid inline style attributes and inline DOM event handlers in page templates.
- Prefer page CSS files over large template-level style blocks.
- Route handlers should render files from `templates/pages/*`.

## Backward Compatibility

Legacy template files remain as wrappers that extend the new `templates/pages/*` templates.
This keeps old references stable while development moves to the new structure.
