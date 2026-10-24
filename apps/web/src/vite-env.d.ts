/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_ENCORE_DATA_MODE?: 'live' | 'fixture';
  readonly VITE_ENCORE_API_ORIGIN?: string;
  readonly VITE_ENCORE_AUTH_LOGIN_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/** true in `vite build` output, false in dev server and tests. */
declare const __ENCORE_BUILD__: boolean;
