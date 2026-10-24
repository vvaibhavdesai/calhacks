export function FixtureBanner({ fixtureId }: { fixtureId: string }) {
  return (
    <div className="fixture-banner" role="note" aria-label="Fixture data notice">
      <strong>FIXTURE DATA</strong> · dev/test only · synthetic <code>{fixtureId}</code> inputs served in-browser, not a
      live API. Runs, settlements, cases, policies and match groups shown here are ILLUSTRATIVE hand-authored samples, not
      backend calculations; payouts and books are not available in this mode.
    </div>
  );
}
