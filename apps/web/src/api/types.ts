/**
 * Friendly aliases over the GENERATED contract types. Do not declare record
 * shapes here by hand: every type must come from ./generated/schema.ts, which
 * is generated from contracts-draft/openapi.draft.yaml (lane-B DRAFT, to be
 * replaced by lane A's packages/contracts OpenAPI).
 */
import type { components, operations, paths } from './generated/schema';
import type { paths as backendPaths } from './generated/backend';

export type { components, operations, paths };
export type Schemas = components['schemas'];

export type SourceRef = Schemas['SourceRef'];
export type ErrorResponse = Schemas['ErrorResponse'];
export type FieldError = Schemas['FieldError'];
export type CommandEnvelope = Schemas['CommandEnvelope'];
export type CommandResult = Schemas['CommandResult'];
export type JobAccepted = Schemas['JobAccepted'];
export type Session = Schemas['Session'];
export type EventEnvelope = Schemas['EventEnvelope'];

export type Vendor = Schemas['Vendor'];
export type Order = Schemas['Order'];
export type Payment = Schemas['Payment'];
export type BankEntry = Schemas['BankEntry'];
export type ProcessorBatch = Schemas['ProcessorBatch'];
export type CashCount = Schemas['CashCount'];
export type SourceDocument = Schemas['SourceDocument'];
export type Festival = Schemas['Festival'];

/**
 * The landing dashboard's aggregate. Unlike every alias above it comes from
 * ./generated/backend.ts, because this endpoint exists only in lane A's real
 * backend and was never part of lane B's draft document -- so there is no
 * Schemas[...] entry to alias, and hand-declaring the shape here is exactly
 * what the header forbids. Generated either way.
 */
export type Dashboard =
  backendPaths['/api/v1/dashboard']['get']['responses']['200']['content']['application/json'];

// Outputs (C03 records + lane-B proposals Q30-Q36).
export type Run = Schemas['Run'];
export type RunStatus = Schemas['RunStatus'];
export type VendorRun = Schemas['VendorRun'];
export type VendorRunState = Schemas['VendorRunState'];
export type VendorMilestone = Schemas['VendorMilestone'];
export type Hold = Schemas['Hold'];
export type MatchGroup = Schemas['MatchGroup'];
export type Settlement = Schemas['Settlement'];
export type SettlementLineItem = Schemas['SettlementLineItem'];
export type SettlementLineTreatment = Schemas['SettlementLineTreatment'];
export type AppliedPolicyRef = Schemas['AppliedPolicyRef'];
export type Case = Schemas['Case'];
export type CaseStatus = Schemas['CaseStatus'];
export type ResolutionOption = Schemas['ResolutionOption'];
export type PolicyVersion = Schemas['PolicyVersion'];
export type PolicyStatus = Schemas['PolicyStatus'];
export type PolicyScope = Schemas['PolicyScope'];
export type ResolveCasePayload = Schemas['ResolveCasePayload'];
export type ApprovePolicyPayload = Schemas['ApprovePolicyPayload'];

// B05 finance outputs (C03 records + lane-B proposals Q38-Q44).
export type Payout = Schemas['Payout'];
export type PayoutState = Schemas['PayoutState'];
export type PayoutGate = Schemas['PayoutGate'];
export type PayoutGateCheck = Schemas['PayoutGateCheck'];
export type PayoutBatch = Schemas['PayoutBatch'];
export type ApprovePayoutPayload = Schemas['ApprovePayoutPayload'];
export type CashPosition = Schemas['CashPosition'];
export type CashForecastPoint = Schemas['CashForecastPoint'];
export type Supplier = Schemas['Supplier'];
export type PurchaseOrder = Schemas['PurchaseOrder'];
export type SupplierInvoice = Schemas['SupplierInvoice'];
export type InvoicePayability = Schemas['InvoicePayability'];
export type SupplierPayment = Schemas['SupplierPayment'];
export type JournalEntry = Schemas['JournalEntry'];
export type JournalLine = Schemas['JournalLine'];
export type TaxReport = Schemas['TaxReport'];
export type TaxReportLine = Schemas['TaxReportLine'];
export type FinancialReport = Schemas['FinancialReport'];
export type FinancialReportSection = Schemas['FinancialReportSection'];
export type FinancialReportLine = Schemas['FinancialReportLine'];

// B06 flight recorder, history, challenges, corrections, branches, replays (lane-B proposals Q46-Q53).
export type EventCategory = Schemas['EventCategory'];
export type ActorType = Schemas['ActorType'];
export type RecordKind = Schemas['RecordKind'];
export type RecordRef = Schemas['RecordRef'];
export type EventList = Schemas['EventList'];
export type HistoricalProjection = Schemas['HistoricalProjection'];
export type HistoricalRow = Schemas['HistoricalRow'];
export type LaterRecordedFact = Schemas['LaterRecordedFact'];
export type Challenge = Schemas['Challenge'];
export type CreateChallengePayload = Schemas['CreateChallengePayload'];
export type PreviewCorrectionPayload = Schemas['PreviewCorrectionPayload'];
export type Correction = Schemas['Correction'];
export type CorrectionItem = Schemas['CorrectionItem'];
export type CorrectionStateGroup = Schemas['CorrectionStateGroup'];
export type CorrectionTreatment = Schemas['CorrectionTreatment'];
export type ApproveCorrectionPayload = Schemas['ApproveCorrectionPayload'];
export type Branch = Schemas['Branch'];
export type CreateBranchPayload = Schemas['CreateBranchPayload'];
export type Replay = Schemas['Replay'];
export type StartReplayPayload = Schemas['StartReplayPayload'];

export type OperationId = keyof operations;

type JsonOf<R> = R extends { content: { 'application/json': infer B } } ? B : never;

/** JSON body of the 200 response of an operation. */
export type OkBody<K extends OperationId> = operations[K]['responses'] extends { 200: infer R }
  ? JsonOf<R>
  : never;

export type QueryOf<K extends OperationId> = operations[K]['parameters'] extends { query?: infer Q }
  ? NonNullable<Q>
  : never;

export type PathParamsOf<K extends OperationId> = operations[K]['parameters'] extends { path: infer P }
  ? P
  : never;

/** Operations whose 200 body is a C03 list envelope {items, next_cursor}. */
export type ListOperationId = {
  [K in OperationId]: OkBody<K> extends { items: unknown[]; next_cursor: string | null } ? K : never;
}[OperationId];

export type ListItem<K extends ListOperationId> = OkBody<K> extends { items: Array<infer I> } ? I : never;

/** Operations that take the C03 command envelope (effectful commands). */
export type CommandOperationId = {
  [K in OperationId]: operations[K] extends { requestBody: { content: { 'application/json': CommandEnvelope } } }
    ? K
    : never;
}[OperationId];
