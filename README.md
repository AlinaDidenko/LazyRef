# LazyRef

**Version 0.13.1 · Windows x64 · English and Ukrainian**

LazyRef helps artists find photo references for a pose, camera angle, or specific detail. Search from a description or an image, compare the results, and save useful references with their sources and match notes. LazyRef uses your installed Codex CLI for search, analysis, and AI pose guides.

## Install on Windows

1. Download and run **LazyRef-0.13.1-Windows-x64-Setup.exe** from the repository's Releases page. The installer supports Windows 10 version 22H2 and Windows 11 on x64 and includes Python and the application libraries.
2. Install the Codex CLI and sign in with `codex login` to use photo search, analysis, and image generation. Check the connection with `codex login status`. Your account needs access to the selected model and tools. [Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli)
3. Open **LazyRef** from the Start menu. Choose **English** or **Ukrainian** in the quick start guide. You can change it later under **Settings → Interface language**. If LazyRef cannot locate Codex automatically, select the native `codex.exe` in Settings.

The installer is unsigned, so Windows may show a publisher warning. `SHA256SUMS.txt` in the release lists the checksums for the installer and the bundled Qt source archive.

## Find references

- **Description → photos.** Describe the pose, view, or scene, choose a reference count, and start the search.
- **AI pose guide → photos.** Generate a few pose guides from a description. Choose a useful guide and search for photographic matches. The generated guides have their own gallery.
- **Image → references.** Use a photo from your computer, a saved reference, an AI guide, or a crop from a gallery. Describe the pose or detail you want to match. You can redo the search with another model using the same image.

The requested count is a target. Search results may include partial matches with an explanation of the differences. The result header can be expanded to see the model, guide, and search statistics.

The first-run guide stays available under **Settings → Open quick start guide** after you dismiss it. **Open folder** opens the current result, selected image, or default destination.

## Work with your library

**Galleries** opens saved references and AI images. You can filter by description, model, name, current or archived status, favorite, and collection. Galleries work locally and display up to 24 images per page. LazyRef scans its results and generation folders, any supported archives in the data folder, and result folders you have selected. Use **Refresh** after adding or restoring files.

Use the context menu on an image to search from it, crop it, reveal it in Explorer, or send it to the Windows Recycle Bin. A source retained for retrying or saving an unfinished session is protected from deletion. Favorites and collection membership are stored in SQLite. The same image can belong to several collections. Separate file copies can carry their own favorites and collections. On NTFS, moving a file within the same volume retains its identity and marks.

Imports accept single-frame PNG, JPEG, and WebP files up to 20 MB and 40 megapixels, with a minimum size of 32 × 32 pixels. An import preserves the original image bytes. The crop tool saves a separate PNG at the original pixel scale, accounts for EXIF rotation, and preserves 16-bit grayscale data. The crop must be at least 32 × 32 pixels and fit the 20 MB limit. You can save it to its gallery or use it to start a detail search.

## Search controls

- **Exact description** weighs clothing, setting, and other details alongside the pose. **Pose and anatomy** prioritizes pose, camera angle, and points of support.
- **Custom priorities** lets you edit up to ten criteria, set each to required, preferred, or ignored, and add your own. At least one criterion must be required. Close matches meet every required criterion, and preferred criteria help rank results.
- **Model and reasoning** can be selected for search and generation. Available models depend on your Codex account. Medium is the default reasoning level. Extra high is sent to the CLI as `xhigh`.
- **Source verification** checks the source and signs of photographic origin. With it turned off, LazyRef labels the origin as unverified and concentrates on the pose. Origin classification is an estimate.
- **Enhanced duplicate filtering** compares reviewed photos for similarity, including some resized, cropped, and watermarked copies. Close matches take priority over partial matches. Among comparable copies, it favors higher resolution. Larger candidates can be reviewed independently within the search budget.
- **Require high-resolution photos** applies a minimum long edge of 1200 px and a minimum short edge of 800 px to full images, including cached images and screenshots. Smaller thumbnails are still used in the screening step. The filter uses native resolution.
- **Wide search** screens thumbnails before detailed review. The default upper bounds are 200 candidates, 40 new detailed reviews, and six cycles. When the first source for a duplicate is unavailable, LazyRef can check another candidate’s own source. A confirmed AI-generated label ends that group’s source checks.

The optional duplicate and resolution filters work locally. The existing budget applies to finding replacements, so a filtered search may take longer or return fewer images.

### Repeat, resume, and save

- **Repeat search** keeps the description, reference image, and confirmed custom criteria, uses the current model, reasoning, and image filters, and starts a new result folder. It does a fresh search with result reuse disabled.
- **Retry analysis** lets you resume from saved candidates. The app reuses assessments when the search conditions, model, reasoning, source evidence, and image files match the saved records.
- **Retry saving** exports an already completed assessment locally. If a session or export write fails, follow the status in the app. Assessments can be recovered from the recovery journal. If both writes fail, keep LazyRef open and retry the write to preserve assessments still in memory.

A direct image download starts with a 45-second budget and can extend to 70 seconds when transfer progress meets the extension criteria. Optional screenshots have a 20-second limit and share a 100-second limit with the direct attempt. Cancellation stops the relevant background process tree. The long-wait control appears after 240 seconds for an individual Codex call. Search and analysis calls have a 500-second timeout, and generation calls have a 600-second timeout.

## Data, network, and usage

The installed app stores its data in `%LOCALAPPDATA%\LazyRef`. Source checkouts use their project folder. Install and uninstall manage the program files, while your library and settings live in the data directory. Select a different location for new search results in Settings.

| Location | Contents |
| --- | --- |
| `.lazyref/settings.json` | App preferences and known result folders |
| `.lazyref/library.sqlite3` | Favorites and collections |
| `.lazyref/startup.log` | Startup and uncaught application errors |
| `references/` or the selected results folder | Search photos, manifests, usage logs, recoverable sessions, and the local search cache |
| `generated_references/` | AI-generated pose guides and their records |

LazyRef can reuse compatible cached search results. Source cache entries expire after one day and other entries after seven days. Cleanup runs at most daily when no active search process is using that cache. Image and thumbnail copies are retained while a current entry refers to them.

Descriptions, selected images, and photos being assessed are sent to Codex for processing. The app downloads photos from public URLs. Local logs can contain descriptions, URLs, file paths, and model responses. Searches, analyses, and generations use your Codex allowance. Gallery browsing, imports, crops, and collection management run locally. Usage totals come from Codex responses and may be incomplete.

For optional screenshots, LazyRef uses Microsoft Edge in a Windows AppContainer with a temporary profile. Its validated transport fetches approved public resources, and Windows restricts direct browser network access. LazyRef skips pages requiring sign-in or a CAPTCHA. Screenshot fallback requires source verification, the screenshot option, and a usable candidate page.

## Run from source

Install a compatible 64-bit Python and the Codex CLI. From the checkout, run `setup.bat` to create `.venv` and install the pinned packages. Open `Start LazyRef.vbs` to start the app. The source launcher and the installed shortcut use a windowed start and launch background workers with the app’s own interpreter.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -B -X utf8 tools/run_offline_tests.py
.\.venv\Scripts\python.exe -B -X utf8 tools/run_download_process_tests.py
.\.venv\Scripts\python.exe -B -X utf8 tools/run_native_browser_tests.py
```

The first test runner blocks real networking and child processes. The other two check native cancellation and the Windows Edge sandbox with synthetic inputs. The smoke and probe scripts are for live checks and can use Codex credits. See the [review guide](REVIEW_GUIDE.md) for test scope.

### Build the Windows installer

Use Inno Setup 7 and a Python environment with pip. Put the exact upstream Qt source archives listed below in one directory. The build verifies the bundled Python and Node.js archives, the Qt source archives, and the pinned Windows wheels. [Inno Setup downloads](https://jrsoftware.org/isdl.php)

- [Qt Base 6.12.0](https://download.qt.io/archive/qt/6.12/6.12.0/submodules/qtbase-everywhere-src-6.12.0.tar.xz)
- [Qt Image Formats 6.12.0](https://download.qt.io/archive/qt/6.12/6.12.0/submodules/qtimageformats-everywhere-src-6.12.0.tar.xz)
- [Qt for Python 6.12.0](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz)

```powershell
.\.venv\Scripts\python.exe -B tools/build_windows_installer.py --iscc "C:\Tools\Inno Setup 7\ISCC.exe" --qt-source-dir "C:\Downloads\Qt sources"
```

Release files are written to `dist/`. The Qt source archive accompanies the installer and the installed third-party notices describe the library licenses and how to replace the bundled Qt libraries. [Third-party notices](installer/THIRD_PARTY_NOTICES.txt)
