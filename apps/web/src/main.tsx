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
    transport = fixtures.createFixtureTransport({ latencyMs: 200 });
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
