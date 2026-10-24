/// <reference types="vitest" />
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

const appRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(appRoot, '../..');
// Compact fixture published by B01. Read-only input for dev/test fixture mode.
export const FIXTURE_API_DIR = path.join(repoRoot, 'data/generator/fixtures/compact_v1/api');

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, appRoot, '');
  const dataMode = env.VITE_ENCORE_DATA_MODE ?? 'live';
  const isBuild = command === 'build';

  if (isBuild && dataMode === 'fixture') {
    // Fixture data is dev/test-only. A build must never ship it or fall back to it.
    throw new Error(
      `Refusing to build with VITE_ENCORE_DATA_MODE=fixture (mode "${mode}"). Fixture mode is dev/test only.`,
    );
  }
  if (dataMode !== 'live' && dataMode !== 'fixture') {
    throw new Error(`VITE_ENCORE_DATA_MODE must be "live" or "fixture", got "${dataMode}"`);
  }

  // In a build, the fixture module is replaced by a stub that contains no data
  // and throws if called. scripts/check-prod-bundle.mjs verifies the result.
  const fixtureModule = isBuild
    ? path.join(appRoot, 'src/fixtures/disabled.ts')
    : path.join(appRoot, 'src/fixtures/compactFixture.ts');

  const proxyTarget = env.ENCORE_DEV_API_PROXY;

  return {
    plugins: [react()],
    resolve: {
      alias: { '@encore/fixture-data': fixtureModule },
    },
    define: {
      __ENCORE_BUILD__: JSON.stringify(isBuild),
    },
    server: {
      fs: { allow: [appRoot, FIXTURE_API_DIR] },
      proxy: proxyTarget ? { '/api': { target: proxyTarget, changeOrigin: true } } : undefined,
    },
    build: {
      sourcemap: false,
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}', 'scripts/**/*.test.{ts,mjs}'],
      css: false,
      env: { TZ: 'UTC' },
    },
  };
});
