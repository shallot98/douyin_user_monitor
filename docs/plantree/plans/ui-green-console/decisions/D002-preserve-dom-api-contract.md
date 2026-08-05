# D002 · Preserve DOM IDs and API call contract

## Decision
Restyle must **not rename** critical element IDs or change `api()` paths/payload shapes. JS handlers stay wired.

## Why
Pages are large inline-script apps; renaming IDs causes silent UI breakage without compile-time checks.

## Practice
1. Diff keep-list of IDs before/after each page
2. Prefer class/CSS changes over DOM id changes
3. If markup wrapper needed, keep id on the same logical control
4. Smoke each page’s primary actions after restyle
