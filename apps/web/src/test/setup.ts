import '@testing-library/jest-dom/vitest';
import { webcrypto } from 'node:crypto';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// Browsers provide crypto.randomUUID in secure contexts; jsdom may not.
if (!globalThis.crypto || typeof globalThis.crypto.randomUUID !== 'function') {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
}

afterEach(() => cleanup());
