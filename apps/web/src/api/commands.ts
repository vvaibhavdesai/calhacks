import type { ApiClient, CommandOutcome } from './client';
import { isApiError } from './errors';
import type { CommandEnvelope, CommandOperationId } from './types';

/**
 * A command prepared ONCE per user intent. command_id and Idempotency-Key are
 * fixed at preparation time and reused for every retry of that intent
 * (CONTRACTS.md C03: same key + same canonical payload returns the original
 * result). A changed payload or a refreshed expected_version is a NEW intent
 * and must be prepared again; reusing a key with a different payload would be
 * rejected by the server with 409.
 */
export interface PreparedCommand {
  readonly commandId: string;
  readonly idempotencyKey: string;
  readonly expectedVersion: number | null;
  readonly envelope: Readonly<CommandEnvelope>;
  /** Serialized envelope, byte-identical across retries. */
  readonly body: string;
}

export function newId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  throw new Error('crypto.randomUUID is unavailable; a secure context is required to send commands.');
}

/** Deterministic JSON with sorted keys, so retries send identical bytes. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) {
      // Money must be integer-cent strings; refuse floats/unsafe numbers in payloads.
      throw new Error(`Refusing non-integer or unsafe number in command payload: ${value}`);
    }
    if (typeof value === 'bigint') throw new Error('Serialize money as integer-cent strings, not BigInt');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(',')}}`;
}

export function prepareCommand(input: {
  expectedVersion: number | null;
  payload: Record<string, unknown>;
  idGenerator?: () => string;
}): PreparedCommand {
  const gen = input.idGenerator ?? newId;
  const commandId = gen();
  const envelope: CommandEnvelope = {
    command_id: commandId,
    expected_version: input.expectedVersion,
    payload: input.payload,
  };
  const body = canonicalJson(envelope);
  return Object.freeze({
    commandId,
    // One key per intent; derived from a separate random id so it is not guessable from command_id.
    idempotencyKey: `idem-${gen()}`,
    expectedVersion: input.expectedVersion,
    envelope: Object.freeze(envelope),
    body,
  });
}

export type SubmissionState =
  | { phase: 'idle' }
  | { phase: 'pending' }
  | { phase: 'succeeded'; outcome: CommandOutcome }
  | { phase: 'failed'; retrySameKey: boolean; error: unknown };

/**
 * Guards a single prepared command against double submission: concurrent
 * submits share one in-flight request; after success the stored outcome is
 * returned without re-sending. Retries after transport/5xx/429 failures reuse
 * the same key. 409/422/403 are final for this intent: the caller must refetch
 * and prepare a new command.
 */
export class CommandSubmission {
  private inFlight: Promise<CommandOutcome> | null = null;
  private state: SubmissionState = { phase: 'idle' };
  private sends = 0;

  constructor(
    private readonly client: ApiClient,
    private readonly op: CommandOperationId,
    readonly prepared: PreparedCommand,
    private readonly path?: Record<string, string>,
  ) {}

  get current(): SubmissionState {
    return this.state;
  }

  /** Number of HTTP sends made (for tests and diagnostics). */
  get sendCount(): number {
    return this.sends;
  }

  submit(): Promise<CommandOutcome> {
    if (this.inFlight) return this.inFlight;
    if (this.state.phase === 'succeeded') return Promise.resolve(this.state.outcome);
    if (this.state.phase === 'failed' && !this.state.retrySameKey) {
      return Promise.reject(this.state.error);
    }
    this.state = { phase: 'pending' };
    this.sends += 1;
    const p = this.client
      .command(this.op, this.prepared, this.path ? { path: this.path } : {})
      .then(
        (outcome) => {
          this.state = { phase: 'succeeded', outcome };
          return outcome;
        },
        (error: unknown) => {
          const retrySameKey =
            isApiError(error) &&
            (error.kind === 'network' ||
              error.kind === 'unavailable' ||
              error.kind === 'rate_limited' ||
              error.kind === 'server');
          this.state = { phase: 'failed', retrySameKey, error };
          throw error;
        },
      )
      .finally(() => {
        this.inFlight = null;
      });
    this.inFlight = p;
    return p;
  }
}
