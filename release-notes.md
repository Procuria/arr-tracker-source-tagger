## 📝 release-notes.md — **v0.6.1**

# v0.6.1 – State-first CQP verification & API call reduction

## Improved

- **State-first Quality Profile enforcement**
  - Upload-aware CQP handling now prefers local state over immediate Arr API calls.
  - Already tracked items are assumed to be correct unless a periodic verification is due.

- **Periodic drift detection**
  - Sonarr/Radarr are queried only after a configurable interval to detect
    manual changes or automation drift.
  - Ensures correctness without constant polling.

- **Automatic state backfill**
  - Items that were already tagged as `uploading` before this change will
    automatically receive the required CQP state fields on the next run.

## New configuration

```env
UPLOADING_CQP_VERIFY_INTERVAL_MINUTES=60
```
Controls how often an Arr item is re-verified while uploading.
Defaults to 60 minutes.

## Why

This release significantly reduces unnecessary Arr API traffic while keeping
the system robust against profile drift caused by manual edits or other tools.

Less noise. Fewer calls. Same guarantees. 