# LazyRef code review guide

Review the current `lazyref/` modules, launchers, installer, dependencies, and tests. The current release is 0.13.1.

For each finding, record its severity, file and line, reproduction steps, observed impact, and proposed fix. Label hypotheses as such. Use synthetic inputs and treat external pages, model responses, imported files, and manifests as untrusted. Use test fixtures and scrub private settings, authorization data, and logs from review artifacts.

## Layout

| Area | Files |
| --- | --- |
| Startup, windows, threads, translations | `app.py`, `lazyref/launcher.py`, `lazyref/ui.py`, `lazyref/i18n.py`, `lazyref/ui_text.py`, `lazyref/guide_ui.py` |
| Windows installer and data paths | `installer/LazyRef.iss`, `tools/build_windows_installer.py`, `lazyref/paths.py` |
| Requests, validation, and profiles | `models.py`, `reasoning.py`, `priorities.py`, `custom_priorities.py` |
| Wide search and thumbnails | `wide_search.py`, `wide_agent.py` |
| Legacy search | `pipeline.py`, `search_agent.py` |
| CLI calls, JSONL, and timeouts | `codex.py`, `processes.py` |
| Downloads, access checks, and browser capture | `network.py`, `access.py`, `download_process.py`, `browser.py`, `capture.py`, `browser_sandbox.py` |
| Image formats, duplicates, and media origin | `images.py`, `photo_filters.py`, `authenticity.py` |
| Importing, generation, cropping, and library | `importing.py`, `generation.py`, `cropping.py`, `library.py`, `library_organization.py` |
| Sessions, recovery, and retry | `review_session.py`, `review_identity.py`, `review_recovery.py`, `retry_analysis.py` |
| Cache, export, and token usage | `search_cache.py`, `cache_maintenance.py`, `exporter.py`, `usage.py` |

Search starts with a description or an image, then proceeds through planning, candidate collection, thumbnail screening, optional source verification, detailed assessment, and export. Unfinished candidates are stored in the review session for a later retry.

## Review priorities

- Check the read-only CLI boundary, inherited configuration and plugin permissions, and how Codex handles instructions in external content. Check process-tree cleanup on cancellation and timeouts.
- Inspect public-address validation, DNS consistency, redirects, and browser subresources. Include IPv4 and IPv6 cases and use synthetic inputs.
- Check session, cache, and export paths, untrusted JSON, Windows junctions, and file integrity. Verify that links restored into the UI still use an allowed URL scheme.
- Check generation artifact ownership against the current request, UUID, path, and file hashes. Include retries, partial generation, and multiple returned images.
- Inspect image size limits, corrupt and animated inputs, load and resize memory bounds, and consistency between the download and screenshot paths.
- Check escaping for Qt labels and HTML export and formula injection protection for CSV.
- Check partial writes, atomic replacement, cancellation, and recoverability of assessments and token counts.
- Check duplicate filtering on resized, cropped, and watermarked copies and distinguish application counts from model self-assessments.
- Check pinned direct and transitive dependencies against current advisories.

## Behavior to preserve

- With source verification off, results use the unverified-origin label and the cache for that mode. Pose matching may accept different clothing or settings when the pose and other mandatory criteria match.
- Repeating a search keeps its reference and confirmed criteria, creates a new result folder, and disables result reuse. The requested result count and review budgets are upper bounds. Incomplete responses remain visible as such.
- Reasoning options map to `low`, `medium`, `high`, and `xhigh`, with Medium as the default.
- Retrying analysis uses saved candidates. Retrying an export uses the saved assessment when identities and settings match. Keep strict prompt fixtures and compatibility with older session formats.
- Language changes update the current GUI and persist the user's choice. The first-run guide lets users choose English or Ukrainian. Install and uninstall manage program files. The installed runtime uses `%LOCALAPPDATA%\LazyRef` for data.

## Regression history

- **0.13.0.** Interface text and common worker messages support English and Ukrainian. The first-run guide can be hidden and reopened from Settings. Search cache cleanup expires entries only when the cache is idle and retains referenced image files. Review sessions accept the current localization metadata while preserving prior data.
- **Windowed startup (0.12.1).** `Start LazyRef.vbs` runs a hidden bootstrap. `launcher.py` uses `CREATE_NO_WINDOW` and redirects standard streams for the app interpreter. That interpreter starts the GUI and its background processes. It logs startup and unhandled errors to a bounded file. See `test_launcher.py` and `test_process_security.py`. Source shortcuts use local paths, and installer shortcuts use the bundled interpreter.
- **LR-R4-06.** Wide search retains downloaded alternatives until source verification succeeds. `DuplicateIndex.add_with_index()` keeps a stable group index, and `replace()` updates the actual image properties after fallback. Verification and assessment use the selected copy's own evidence. See `test_duplicate_source_fallback.py` and `test_dedup_resolution.py`.
- **LR-R4-05.** `display_copy_for_resize()` prepares a display copy of large 16-bit grayscale images. It retains the source range during resize and converts tRNS transparency to RGBA before filtering. Crop storage preserves 16-bit values and source pixels. See `test_large_16bit_preview.py`.
- **LR-R4-04.** `ReviewSession.load_metadata()` reads the bounded session metadata. The worker takes `ReviewLock` before full file and recovery validation, then sends the prepared request to the UI. The explicit save or analysis choice passes through the worker. See `test_review_metadata_ui.py`.
- **LR-R4-03.** `result_cleanup.py` closes each resource independently and records cleanup errors alongside completed results. The search paths retain the result folder, photos, statistics, cancellation state, and retry details for the UI. See `test_result_cleanup.py` and the process transport tests.
- **LR-R4-02.** `Asset.review_owned` marks items retained for review recovery. The UI and Recycle Bin operation protect those files. Successful exported files, imports, and crops are independently managed. See `test_session_delete_protection.py`.
- **LR-R4-01.** `save_only` carries the explicit export-only intent from the UI. `analysis_settings` records the actual model and reasoning before an analysis retry. Export restoration verifies the saved data and completes locally when assessments match. See `test_save_only_review.py` and `test_export_recovery.py`.
- **LR-R3-01.** `review_identity.py` binds assessments to the search conditions, image hashes, and source evidence. `review_recovery.py` saves assessments to a bounded recovery journal. Session writes use a unique temporary file, fsync, and replacement. A failed write preserves available assessments and stops fresh model calls until recovery can proceed. See the review identity, recovery, reuse, and atomic-write tests.
- **LR-R2-01.** Direct downloads start with 45 seconds and may extend to 70 seconds with qualifying progress. They share a 100-second budget with the 20-second screenshot attempt. The HTTP worker uses `ProcessTree` so cancellation covers blocked DNS and reads. See the download budget, transport, and native process tests.
- **LR-R2-05–10.** Generation manifests are atomic, the crop editor retains its lifecycle, 16-bit grayscale crops preserve pixels, larger duplicate copies can be assessed independently, invalid settings are backed up, and the probes manage process trees. See the generation write, crop, deduplication, settings recovery, and probe cleanup tests.
- **0.12.** `enhanced_dedup` and `high_quality_only` are independent options and default to off. Image filters use native pixel dimensions and locally compare candidate copies, including some crops. Filters apply to full images, while thumbnails support screening. Session retries use their saved options. See `test_photo_filters.py` and `test_ui.py`.
- **0.11.1.** The welcome prompt, the waiting area, and results use distinct `QStackedWidget` states. Failures and empty results have their own messages.
- **0.11.** A separate SQLite database stores favorites and collections through parameterized transactions. File ID, size, and modified time distinguish replacements and copies and support same-volume moves. See `test_library_organization.py` and `test_organization_ui.py`.
- **0.10.** Custom priorities let users confirm required, preferred, and ignored criteria after planning. The confirmed plan drives search, assessment, cache keys, and retries. Local imports preserve the bytes of PNG, JPEG, and WebP files and create an import manifest. See `test_custom_priorities.py`, `test_priorities_ui.py`, and `test_importing.py`.
- **Library and crop support.** The galleries index current and archived results locally, verify file boundaries, and use the Recycle Bin. The crop editor saves a new PNG and manifest with EXIF-aware coordinates. `reference_mode=visual` carries general visual references through search, cache, review, and export. See the library, cropping, and visual reference tests.

## Local verification

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -X utf8 tools/run_offline_tests.py
.\.venv\Scripts\python.exe -B -X utf8 tools/run_download_process_tests.py
.\.venv\Scripts\python.exe -B -X utf8 tools/run_native_browser_tests.py
```

The offline runner blocks real networking and child processes and redirects test data. Native runners use synthetic inputs for separate downloads and AppContainer tests. `test_capture.py` is an older headless Edge test for framing and may be skipped on systems without Edge.

The smoke and probe scripts and the model matrix `run` and `worker` commands make live requests. Use the matrix's `report` command for local reconstruction. The `init` command can prepare a new set from existing local guide images.

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/run_model_matrix.py init --root references/Model_tests_new --selection-dir path/to/pose-guides
```
