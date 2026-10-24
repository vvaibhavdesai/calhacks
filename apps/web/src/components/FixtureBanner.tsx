export function FixtureBanner({ fixtureId }: { fixtureId: string }) {
  return (
    <div className="fixture-banner" role="note" aria-label="Fixture data notice">
      <strong>FIXTURE DATA</strong> · dev/test only · synthetic <code>{fixtureId}</code> inputs served in-browser, not a
      live API. Outputs shown here (runs, settlements, cases, policies, match groups, payouts, cash, payables, journal, tax
      drafts and reports) are ILLUSTRATIVE hand-authored samples, not backend calculations.
    </div>
  );
}
