/**
 * `Case.evidence_refs` is typed as `SourceRef[]` (the draft fixture shape:
 * source_id/locator_kind/locator/quoted_excerpt), but the real backend's
 * DELIVERY_SHORTFALL/SLA_POTENTIAL_CREDIT cases put a DIFFERENT-shaped
 * marker as the first entry -- {ref_kind: "supplier_invoice", ref_id, role}
 * -- identifying which invoice the case is about. Mixed-shape array, same
 * draft/live mismatch already handled elsewhere in this codebase via an
 * `as unknown as` cast (see Cases.tsx's financial_impact/resolution_decision
 * reads); this is that same pattern, scoped to one helper so both screens
 * that need it agree on the shape.
 */
export function invoiceIdFromCaseEvidence(evidenceRefs: unknown): string | null {
  if (!Array.isArray(evidenceRefs)) return null;
  for (const ref of evidenceRefs) {
    const r = ref as { ref_kind?: unknown; ref_id?: unknown; role?: unknown };
    if (r.ref_kind === 'supplier_invoice' && r.role === 'subject' && typeof r.ref_id === 'string') {
      return r.ref_id;
    }
  }
  return null;
}
