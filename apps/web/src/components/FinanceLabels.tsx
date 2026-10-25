/**
 * Required qualifiers (AGENT_HANDOFF, C05): payouts and supplier payments are
 * SIMULATED, tax output is DRAFT, NOT FILED. Text carries the meaning; these
 * render in live mode too, not only with fixtures.
 */
export const SIMULATED_TEXT = 'Payments here are simulated and nothing reaches a bank.';
export const DRAFT_TAX_TEXT = 'DRAFT, NOT FILED';

export function SimulatedBanner({ what = 'Payments on this screen' }: { what?: string }) {
  return (
    <p className="label-banner label-simulated" role="note">
      <strong>Simulated payments.</strong> {what} go only to a simulated rail. Nothing reaches a bank, a card network, or a
      real vendor or supplier account.
    </p>
  );
}

export function DraftTaxBanner() {
  return (
    <p className="label-banner label-draft" role="note">
      <strong>{DRAFT_TAX_TEXT}.</strong> A draft computed with configurable demo assumptions for a fictional festival. It is
      not a tax return, it has not been filed, and it claims no legal compliance.
    </p>
  );
}
