import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiClient, liveTransport, type Transport } from './api/client';
import { App } from './app/App';
import { readConfig } from './config/env';
import './styles/app.css';

async function bootstrap() {
  const config = readConfig();
  let transport: Transport;
  let fixtureId: string | null = null;

  if (config.dataMode === 'fixture') {
    // Dev/test only. In a build this alias resolves to src/fixtures/disabled.ts
    // and vite.config.ts refuses fixture mode, so no fixture data can ship.
    const fixtures = await import('@encore/fixture-data');
    // Inputs from compact_v1 plus ILLUSTRATIVE hand-authored outputs for the B04/B05 screens.
    // The dev session holds the decision capabilities so the workflow can be exercised;
    // the transport still enforces them per record, as the real server would.
    // B06: VITE_ENCORE_FIXTURE_STORY=night3 (default in .env.fixtures) overlays the illustrative
    // Night 3 story and enables the flight-recorder/correction stand-in; night1 keeps B04/B05 data only.
    transport = fixtures.createFixtureTransport({
      latencyMs: 200,
      illustrativeOutputs: true,
      story: import.meta.env.VITE_ENCORE_FIXTURE_STORY === 'night1' ? 'night1' : 'night3',
      capabilities: [
        'fixture.read', 'case.resolve', 'policy.approve', 'payout.approve_above_limit',
        'challenge.create', 'correction.approve', 'branch.create', 'replay.start',
      ],
    });
    fixtureId = fixtures.FIXTURE_ID;
  } else {
    // Live mode never falls back to fixtures: API failures surface as error states.
    transport = liveTransport();
  }

  const client = new ApiClient({ transport, origin: config.apiOrigin });
  const root = document.getElementById('root');
  if (!root) throw new Error('#root missing');
  createRoot(root).render(
    <StrictMode>
      <App services={{ config, client, fixtureId }} />
    </StrictMode>,
  );
}

bootstrap().catch((e: unknown) => {
  const root = document.getElementById('root');
  if (root) {
    root.textContent = `Encore failed to start: ${e instanceof Error ? e.message : 'unknown error'}`;
  }
});
