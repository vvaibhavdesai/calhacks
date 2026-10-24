import '@testing-library/jest-dom/vitest';
import { webcrypto } from 'node:crypto';
import { cleanup, configure } from '@testing-library/react';
import { afterEach } from 'vitest';

// Browsers provide crypto.randomUUID in secure contexts; jsdom may not.
if (!globalThis.crypto || typeof globalThis.crypto.randomUUID !== 'function') {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
}

// B04 detail pages chain several fetches (case -> candidate policy -> lookups);
// under a parallel full-suite run jsdom can exceed the 1 s default.
configure({ asyncUtilTimeout: 4000 });

afterEach(() => cleanup());
