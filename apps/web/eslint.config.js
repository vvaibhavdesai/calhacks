import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

/**
 * Money guard: outside src/lib/money.ts, code must not do arithmetic on
 * *_cents values, convert them to numbers, or create BigInts. The UI displays
 * server-calculated amounts; it never computes them (CONTRACTS.md C01,
 * TASK_IMPLEMENTATION B03 "no financial formulas duplicated in UI").
 */
const CENTS = '/_cents$/';
const moneyGuard = [
  {
    selector: `BinaryExpression[operator=/^[-+*\\/%]$/] > MemberExpression[property.name=${CENTS}]`,
    message: 'No arithmetic on money (*_cents). Display server values via <Money>/formatUsd.',
  },
  {
    selector: `BinaryExpression[operator=/^[-+*\\/%]$/] > Identifier[name=${CENTS}]`,
    message: 'No arithmetic on money (*_cents). Display server values via <Money>/formatUsd.',
  },
  {
    selector: `AssignmentExpression[operator=/^[-+*\\/%]=$/] > MemberExpression[property.name=${CENTS}]`,
    message: 'No arithmetic on money (*_cents).',
  },
  {
    selector: `CallExpression[callee.name=/^(Number|parseFloat|parseInt|BigInt)$/] > MemberExpression[property.name=${CENTS}]`,
    message: 'Do not convert money to numbers outside src/lib/money.ts.',
  },
  {
    selector: `CallExpression[callee.name='BigInt']`,
    message: 'BigInt is reserved for src/lib/money.ts (formatting/comparison only).',
  },
  {
    selector: `CallExpression[callee.property.name='reduce'] MemberExpression[property.name=${CENTS}]`,
    message: 'Do not total money client-side; use server-provided totals.',
  },
];

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage', 'src/api/generated'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { globals: { ...globals.browser }, ecmaVersion: 2022, sourceType: 'module' },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-restricted-syntax': ['error', ...moneyGuard],
    },
  },
  {
    // Production code must reach fixtures only through the '@encore/fixture-data' alias
    // (which a build replaces with a data-free stub).
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/fixtures/**', 'src/**/*.test.{ts,tsx}', 'src/test/**'],
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            { group: ['**/fixtures/*', '**/data/generator/**'], message: "Import fixtures only via '@encore/fixture-data'." },
          ],
        },
      ],
    },
  },
  {
    files: ['src/lib/money.ts'],
    rules: { 'no-restricted-syntax': 'off' },
  },
  {
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**', 'scripts/**'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
    rules: { 'no-restricted-syntax': 'off' },
  },
  {
    files: ['*.{js,mjs,ts}', 'scripts/**/*.mjs'],
    languageOptions: { globals: { ...globals.node } },
  },
);
