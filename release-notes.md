# Release v0.5 — Regression & Stability Fixes

This release focuses entirely on **stability, regression fixes, and hardening** of existing features introduced in earlier versions.

No new functionality is introduced in v0.5.

---

## 🛠 Fixed: ArrClient method regressions

Several runtime errors were caused by methods not being bound correctly to `ArrClient` due to earlier refactors and indentation changes.

The following methods are now **guaranteed to exist at runtime** via explicit monkeypatching:

- `apply_source_tag()`  
- `fetch_history()`  

This prevents runtime failures such as:
- `AttributeError: 'ArrClient' object has no attribute 'apply_source_tag'`
- `AttributeError: 'ArrClient' object has no attribute 'fetch_history'`

---

## 🔁 Backfill reliability improvements

- `/backfill/history` is now robust against missing ArrClient methods
- History backfill no longer aborts due to internal method resolution issues
- Webhook-based tagging (`/tag`) and history backfill now share a consistent, hardened ArrClient surface

---

## 🧠 Design note

These fixes intentionally favor **runtime safety over structural refactors** to avoid further regressions.  
Monkeypatching is used deliberately to ensure backward compatibility and predictable behavior across deployments.

A future release may clean up duplicated or mis-indented legacy methods, but v0.5 prioritizes correctness and stability.

---

## 🏷 Version

- **Tag:** v0.5
- **Scope:** Regression fixes only
