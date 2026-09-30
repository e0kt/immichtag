# Feature Specification: Delete and Download from Filter Results

**Feature Branch**: `002-filter-delete-download`

**Created**: 2026-09-29

**Status**: Draft

**Input**: User description: "enable delete or direct download from the filter result, and enable batch delete and download"

## Background

The web page (`immichtag-web`) shows the images that match the current filters as a thumbnail grid, and a click opens one image with its tags. Today the only thing the page can do with a result is **Export**: copy the matches into a folder on this Mac, subject to a count cap, a size limit and optional compression.

Two jobs are missing:

- **Getting an image out right away.** To save a single picture, the user has to set up an export folder, run an export and open that folder.
- **Removing unwanted images.** Filtering is often how the user finds the images they don't want, such as duplicates, bad crops or unwanted content. Today they have to find those images again in Immich to delete them.

The library is Immich's own upload folder, so an image is one of Immich's assets. Deleting it means deleting it in Immich, not just removing the file from disk.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Download a single image from the results (Priority: P1)

While browsing the results, the user sees an image they want and downloads it through the browser in one action, from the thumbnail or from the full-image viewer. They get the original file with its original name and don't have to pick an output folder.

**Why this priority**: It's the simplest and most frequent use case, it can't destroy anything, and it's useful even if nothing else in this feature ships.

**Independent Test**: Filter the library, click download on one result, and check that the browser saves a file byte-identical to the original under the original file name.

**Acceptance Scenarios**:

1. **Given** a filter showing results, **When** the user clicks download on a thumbnail, **Then** the browser saves that image's original file under its original name.
2. **Given** the full-image viewer is open, **When** the user clicks download, **Then** the browser saves the image being viewed.
3. **Given** a download, **When** it completes, **Then** the library, the filter, the selection and the results grid are unchanged.

---

### User Story 2 - Select several results and download them together (Priority: P2)

The user selects several images in the grid, one by one or with "select all matching", and downloads them all at once as a single archive.

**Why this priority**: It builds on Story 1 and replaces the export-folder round trip for ad-hoc picks, but it needs the selection mechanism first.

**Independent Test**: Select 5 images, download them, and check that one archive arrives containing exactly those 5 original files.

**Acceptance Scenarios**:

1. **Given** the results grid, **When** the user turns on selection and clicks thumbnails, **Then** each clicked image is visibly marked as selected, and a bar shows how many are selected with Download and Delete actions.
2. **Given** a selection of N images, **When** the user clicks Download, **Then** the browser saves one archive containing exactly those N original files.
3. **Given** a filter matching M images, **When** the user chooses "select all matching", **Then** all M are selected, including ones not yet loaded into the grid, and the count shows M.
4. **Given** two selected images with the same file name, **When** they are downloaded together, **Then** both are in the archive under distinct names.
5. **Given** a selection, **When** the user changes the filters, **Then** the selection is cleared, so an action never touches images the user can no longer see.

---

### User Story 3 - Delete one image or a selection (Priority: P2)

The user deletes a single image from the viewer, or deletes the current selection, including "all matching". The images go to Immich's trash, disappear from the results and counts, and can be restored from Immich's trash within Immich's retention period.

**Why this priority**: It's the main reason this feature was asked for, but it's destructive, so it ranks after the safe download path and depends on the Immich connection.

**Independent Test**: Delete one image from the viewer. Confirm that it's gone from the grid and the match count, that Immich shows it in the trash, and that restoring it in Immich brings it back after a rescan.

**Acceptance Scenarios**:

1. **Given** the viewer is open, **When** the user clicks Delete and confirms, **Then** the image moves to Immich's trash and disappears from the grid, the match count and the tag counts.
2. **Given** a selection of N images, **When** the user clicks Delete, **Then** a confirmation shows N and a sample of thumbnails, and nothing is deleted until the user confirms.
3. **Given** a selection larger than 100 images, **When** the user deletes it, **Then** the confirmation also requires typing the number N (or an equivalent deliberate step).
4. **Given** a confirmed batch delete, **When** it runs, **Then** progress is shown. When it ends, the page reports how many were deleted and lists any that failed with the reason.
5. **Given** Immich isn't configured or can't be reached, **When** the user looks at the results, **Then** the delete actions are disabled, with an explanation, and download still works.
6. **Given** a trashed image, **When** the user restores it in Immich and rescans, **Then** it appears in the results again with its tags.

---

### Edge Cases

- **A file was removed or changed after indexing.** The download fails for that image with a clear message. A batch download skips it and names it in a note inside the archive.
- **An image has no matching Immich asset**, for example because Immich hasn't discovered a new upload yet. It can't be deleted. The batch report lists it as "not in Immich" and the rest are still deleted.
- **A very large batch download**, such as tens of thousands of images and tens of GB. The user sees the total size before the download starts and is warned above a threshold. The page stays usable while the archive streams.
- **A delete while a tagging run, a sync or an export is in progress.** The delete is refused with a message until that job finishes, so the images being deleted aren't also being written.
- **A delete of images that are also marked "pending sync".** They're dropped from the pending list and never synced.
- **The same image is open in the viewer when it's deleted by a batch.** The viewer closes.
- **The user reloads the page during a batch delete.** The page reattaches to the running delete and shows its progress, the same way it does for tagging.
- **Deleted images are no longer in the results.** A later Export, "select all matching" or tag count doesn't include them.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The results grid and the full-image viewer MUST each offer a one-click download of a single image, which delivers the original, unmodified file under its original file name through the browser.
- **FR-002**: Users MUST be able to select and deselect individual results, see which are selected, and see the count selected.
- **FR-003**: Users MUST be able to select all images matching the current filters, including images not yet loaded in the grid, and clear the selection.
- **FR-004**: The selection MUST be cleared whenever the filters, the match mode or the exclusions change.
- **FR-005**: Users MUST be able to download the selection as one archive of the original files. Name collisions inside the archive MUST be resolved without losing any file.
- **FR-006**: Before a batch download starts, the page MUST show the number of images and their total size.
- **FR-007**: Users MUST be able to delete a single image from the viewer, and delete the current selection.
- **FR-008**: Delete MUST move images to Immich's trash, so they stay recoverable in Immich for its trash retention period. It MUST NOT permanently delete them or remove files behind Immich's back.
- **FR-009**: Every delete MUST require an explicit confirmation that states the number of images. A delete of more than 100 images MUST require an additional deliberate step, such as typing the count.
- **FR-010**: A batch delete MUST show progress. It MUST end with a report of how many were deleted and which failed and why, and one failure MUST NOT stop the rest.
- **FR-011**: After a delete, the deleted images MUST no longer appear in the results, the match count, tag counts, "select all" or exports, without a manual rescan.
- **FR-012**: Delete MUST be unavailable, with a visible reason, when Immich isn't configured or isn't reachable. Download MUST NOT depend on Immich.
- **FR-013**: A delete MUST NOT start while a tagging run, sync, cleanup or export is in progress, and none of those may start during a delete.
- **FR-014**: Each delete run MUST be recorded locally, with the time and which images were deleted, so the user can find what to restore in Immich.
- **FR-015**: Deleted images MUST be removed from the list of images pending an Immich sync.

### Key Entities

- **Selection**: the set of result images the user has chosen. It's either an explicit list, or "all matching the current filters" plus a count. It belongs to one filter state and is discarded when the filters change.
- **Download bundle**: the archive produced for a batch download. It holds the original files under unique names, plus an optional note listing skipped files.
- **Delete job**: one confirmed delete run. It has a list of target images, progress, per-image outcomes (deleted, not in Immich or failed with a reason) and a start/end time, and it's recorded in the delete log.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can save one image from the results in at most 2 clicks, with no folder setup.
- **SC-002**: A user can download a hand-picked set of 20 images as one file in under 30 seconds of interaction.
- **SC-003**: 100% of downloaded files are byte-identical to the originals.
- **SC-004**: 100% of deleted images can be found in Immich's trash and restored from there. No delete leaves Immich pointing at a missing file.
- **SC-005**: No image is ever deleted without a confirmation that stated the exact count. Deleting 1,000 images takes one confirmation and reports completion without further input.
- **SC-006**: Right after a delete finishes, the match count and grid reflect it within 2 seconds, with no manual rescan.

## Assumptions

- The page stays a single-user tool on this Mac (loopback only, as today). No new access control is added, but delete makes that default more important, and the README will say so.
- "Delete" means Immich's trash, which is reversible, not permanent deletion. Emptying the trash stays in Immich.
- The existing Immich API key has, or can be given, permission to trash assets. `immichtag-sync status` will report whether it does.
- Downloads are always originals. The Export section's size limit, compression and max-images settings don't apply to downloads, and Export is kept unchanged for those needs.
- A batch download is a single archive. A single-image download is the bare file.
- Delete and download are added to the web page only. The command-line filter tool is out of scope for this feature.
