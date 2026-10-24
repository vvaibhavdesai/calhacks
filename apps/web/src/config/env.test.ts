// @vitest-environment node
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it, vi } from 'vitest';
import { readConfig } from './env';
import viteConfig from '../../vite.config';

type ViteConfigFn = (env: { command: 'build' | 'serve'; mode: string }) => {
  resolve: { alias: Record<string, string> };
};
const configFn = viteConfig as unknown as ViteConfigFn;
const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

describe('fixture-mode gating', () => {
  it('defaults to live mode', () => {
    expect(readConfig({} as ImportMetaEnv)).toEqual({ dataMode: 'live', apiOrigin: '', authLoginUrl: null });
  });

  it('accepts fixture mode outside a build', () => {
    expect(readConfig({ VITE_ENCORE_DATA_MODE: 'fixture' } as ImportMetaEnv).dataMode).toBe('fixture');
  });

  it('refuses fixture mode at runtime in a production build', () => {
    vi.stubGlobal('__ENCORE_BUILD__', true);
    try {
      expect(() => readConfig({ VITE_ENCORE_DATA_MODE: 'fixture' } as ImportMetaEnv)).toThrow(/not available in a production build/);
      expect(readConfig({ VITE_ENCORE_DATA_MODE: 'live' } as ImportMetaEnv).dataMode).toBe('live');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('rejects unknown modes and origins with paths', () => {
    expect(() => readConfig({ VITE_ENCORE_DATA_MODE: 'mock' } as unknown as ImportMetaEnv)).toThrow();
    expect(() => readConfig({ VITE_ENCORE_API_ORIGIN: 'https://api.example/api/v1' } as ImportMetaEnv)).toThrow();
    expect(readConfig({ VITE_ENCORE_API_ORIGIN: 'https://api.example/' } as ImportMetaEnv).apiOrigin).toBe('https://api.example');
  });

  it('vite build refuses the fixtures mode', () => {
    expect(() => configFn({ command: 'build', mode: 'fixtures' })).toThrow(/Refusing to build/);
  });

  it('vite build aliases fixture data to the data-free stub; dev/test use the real fixture', () => {
    expect(configFn({ command: 'build', mode: 'production' }).resolve.alias['@encore/fixture-data']).toBe(
      path.join(appRoot, 'src/fixtures/disabled.ts'),
    );
    expect(configFn({ command: 'serve', mode: 'fixtures' }).resolve.alias['@encore/fixture-data']).toBe(
      path.join(appRoot, 'src/fixtures/compactFixture.ts'),
    );
  });

  it('the production stub carries no data and fails loudly', async () => {
    const stub = await import('../fixtures/disabled');
    expect(stub.FIXTURE_ID).toBeNull();
    expect(() => stub.createFixtureTransport()).toThrow(/not included in production builds/);
  });
});
