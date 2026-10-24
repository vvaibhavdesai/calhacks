/**
 * Required qualifiers (AGENT_HANDOFF, C05): payouts and supplier payments are
 * SIMULATED, tax output is DRAFT, NOT FILED. Text carries the meaning; these
 * render in live mode too, not only with fixtures.
 */
export const SIMULATED_TEXT = 'SIMULATED — no real money moves';
export const DRAFT_TAX_TEXT = 'DRAFT, NOT FILED';

export function SimulatedBanner({ what = 'Payments on this screen' }: { what?: string }) {
  return (
    <p className="label-banner label-simulated" role="note">
      <strong>{SIMULATED_TEXT}.</strong> {what}: sent only to a simulated rail. Nothing is sent to a bank, card network or
      real vendor or supplier account.
    </p>
  );
}

export function DraftTaxBanner() {
  return (
    <p className="label-banner label-draft" role="note">
      <strong>{DRAFT_TAX_TEXT}.</strong> A draft computed with configurable DEMO assumptions for a fictional festival. It is
      not a tax return, has not been filed and is not a claim of legal compliance.
    </p>
  );
}
