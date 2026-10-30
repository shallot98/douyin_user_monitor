# Topic · Risks & rollback

## Risks
| Risk | Impact | Mitigation |
|------|--------|------------|
| Page `:root` overrides fight `common.css` | Half-themed / unreadable | Strip page light tokens first |
| Accidental ID rename | Silent JS break | ID inventory diff |
| statistics density + dark theme contrast | Unreadable charts/text | Tokenize colors; check contrast on real data |
| Bottom tabs overlap content | Obscured CTAs | `main-content` bottom padding + safe-area |
| Large HTML merge conflicts | Hard review | Page-by-page commits |

## Rollback
- Git revert per-page commits
- `common.css` can revert independently if pages still self-contained (ideal intermediate state avoids hard break)
- Mockups remain available as design fallback, not runtime

## Partial ship safety
If only dashboard lands: ensure its local CSS does not depend on unfinished statistics-only classes; shared tokens must stay backward compatible enough for old pages **or** land cookies in same commit to avoid two shells.
