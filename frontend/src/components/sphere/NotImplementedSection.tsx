type NotImplementedSectionProps = {
  /** The component name the descriptor asked for but that the section registry
   * does not know. Shown to the user so an out-of-date frontend degrades
   * visibly instead of silently dropping the section or crashing the overlay. */
  name: string;
};

/**
 * Fallback section rendered when a descriptor's `component` is absent from the
 * section registry. Mirrors the amber `UnknownComponent` block in `Dispatcher`
 * — a visible, contained warning rather than a thrown error or the raw props.
 * The raw props are deliberately NOT rendered (they may carry large / sensitive
 * data) so an unknown section can never leak or blow up the layout.
 *
 * PRD 0010 robustness bar: an unknown section never crashes rendering.
 *
 * PRD: prd/0010-adaptive-composite-ui.md — Issue: issues/0066-sections-list-pipeline-markdown.md
 */
export function NotImplementedSection({ name }: NotImplementedSectionProps) {
  return (
    <div
      className="ov-section-unsupported rounded-md border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
      role="note"
    >
      <div className="font-medium">
        Section non supportée : <code className="font-mono">{name}</code>
      </div>
      <div className="mt-1 opacity-80">
        Cette interface ne connaît pas encore ce type de section. Mets l'application à jour pour
        l'afficher.
      </div>
    </div>
  );
}
