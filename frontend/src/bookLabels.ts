// Phase 7 (composer-filter-debug): shared per-book label lookup (DECIDE C3).
// Extracted out of QuestionLine's scope chip (Phase 6) so QuestionLine,
// SourcesList, and Phase 7's FilterControl all resolve part_label/
// chapter_label off the SAME BookNode lookup instead of three independent
// copies that could silently drift (e.g. one component fixing a label bug
// the other two never get). Never hardcode "Part" -- some books (Karamazov)
// use "Book".

import type { BookNode, StructureTree } from "./api/types"

/** Resolve a book's node from the /books tree by its display-name key.
 * Returns undefined if the tree hasn't loaded yet or the name doesn't
 * resolve -- callers render nothing/a fallback rather than fabricate a
 * label off a missing node. */
export function resolveBookNode(
  tree: StructureTree | null,
  book: string | undefined,
): BookNode | undefined {
  if (!book || !tree) return undefined
  return tree[book]
}

/** "<part_label> <part>", e.g. "Part 2" or "Book 2" -- per-book, never a
 * hardcoded "Part". Returns null when there's no part to label. */
export function formatPart(node: BookNode, part: number | null | undefined): string | null {
  if (part === null || part === undefined) return null
  return `${node.part_label} ${part}`
}

/** Rendered in place of a null `chapter_label` (The Idiot, Notes from the
 * Underground -- unlabeled bare-roman-numeral chapters). Interpolating the
 * raw null produced "null 5" in the source locator. */
export const DEFAULT_CHAPTER_LABEL = "Chapter"

/** "<chapter_label> <chapter>", e.g. "Chapter 3". Returns null when there's
 * no chapter to label. */
export function formatChapter(node: BookNode, chapter: number | null | undefined): string | null {
  if (chapter === null || chapter === undefined) return null
  return `${node.chapter_label ?? DEFAULT_CHAPTER_LABEL} ${chapter}`
}
