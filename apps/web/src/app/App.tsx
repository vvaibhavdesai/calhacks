import { useEffect, useState } from 'react';
import { RouterProvider, createBrowserRouter, createMemoryRouter, type RouteObject } from 'react-router-dom';
import { isLive } from '../api/operations';
import { EventStreamClient, liveStreamOpener } from '../api/sse';
import { CaseDetail, CasesList } from '../screens/Cases';
import { CashScreen } from '../screens/Cash';
import { JournalDetail, JournalList } from '../screens/Journal';
import { InvoiceDetail, PayablesScreen } from '../screens/Payables';
import { PayoutDetail, PayoutsList } from '../screens/Payouts';
import { PayoutTrace } from '../screens/PayoutTrace';
import { BranchDetail, BranchesScreen } from '../screens/Branches';
import { ChallengeDetail, ChallengeNew, CorrectionDetail } from '../screens/Corrections';
import { HistoryScreen } from '../screens/History';
import { EventDetail, RecorderTimeline } from '../screens/Recorder';
import { ReplayDetail, ReplaysScreen } from '../screens/Replays';
import { ReportDetail, ReportsList } from '../screens/Reports';
import { TaxDetail, TaxList } from '../screens/Tax';
import { Overview } from '../screens/Overview';
import { PoliciesList, PolicyDetail } from '../screens/Policies';
import { MatchGroupDetail, ReconciliationList } from '../screens/Reconciliation';
import { RunDetail, RunsList } from '../screens/Runs';
import { SettlementDetail, SettlementsList } from '../screens/Settlements';
import { NotFound } from '../screens/NotFound';
import { PlaceholderScreen } from '../screens/PlaceholderScreen';
import { SourceRecords } from '../screens/SourceRecords';
import { AppProvider, useApp, type AppServices } from './AppContext';
import { Layout } from './Layout';
import { RouteError } from '../components/RouteError';
import { PLACEHOLDERS } from './navigation';

const ROUTER_FUTURE = {
  v7_fetcherPersist: true,
  v7_normalizeFormMethod: true,
  v7_partialHydration: true,
  v7_relativeSplatPath: true,
  v7_skipActionErrorRevalidation: true,
} as const;

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <Layout />,
    // Scoped to the children so the shell and navigation survive a screen
    // that throws; without it React Router replaces the whole app.
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Overview /> },
      { path: 'runs', element: <RunsList /> },
      { path: 'runs/:runId', element: <RunDetail /> },
      { path: 'reconciliation', element: <ReconciliationList /> },
      { path: 'reconciliation/:groupId', element: <MatchGroupDetail /> },
      { path: 'settlements', element: <SettlementsList /> },
      { path: 'settlements/:settlementId', element: <SettlementDetail /> },
      { path: 'cases', element: <CasesList /> },
      { path: 'cases/:caseId', element: <CaseDetail /> },
      { path: 'policies', element: <PoliciesList /> },
      { path: 'policies/:policyVersionId', element: <PolicyDetail /> },
      // B05 finance screens
      { path: 'payouts', element: <PayoutsList /> },
      { path: 'payouts/:payoutId', element: <PayoutDetail /> },
      { path: 'payouts/:payoutId/trace', element: <PayoutTrace /> },
      { path: 'cash', element: <CashScreen /> },
      { path: 'payables', element: <PayablesScreen key="sup" tab="" /> },
      { path: 'payables/purchase-orders', element: <PayablesScreen key="po" tab="purchase-orders" /> },
      { path: 'payables/invoices', element: <PayablesScreen key="inv" tab="invoices" /> },
      { path: 'payables/payments', element: <PayablesScreen key="pay" tab="payments" /> },
      { path: 'payables/invoices/:invoiceId', element: <InvoiceDetail /> },
      { path: 'journal', element: <JournalList /> },
      { path: 'journal/:entryId', element: <JournalDetail /> },
      { path: 'tax', element: <TaxList /> },
      { path: 'tax/:taxId', element: <TaxDetail /> },
      { path: 'reports', element: <ReportsList /> },
      { path: 'reports/:reportId', element: <ReportDetail /> },
      // B06 flight recorder, history, replays, branches, challenges and corrections
      { path: 'recorder', element: <RecorderTimeline /> },
      { path: 'recorder/events/:eventId', element: <EventDetail /> },
      { path: 'recorder/history', element: <HistoryScreen /> },
      { path: 'recorder/replays', element: <ReplaysScreen /> },
      { path: 'recorder/replays/:replayId', element: <ReplayDetail /> },
      { path: 'recorder/branches', element: <BranchesScreen /> },
      { path: 'recorder/branches/:branchId', element: <BranchDetail /> },
      { path: 'recorder/challenges/new', element: <ChallengeNew /> },
      { path: 'recorder/challenges/:challengeId', element: <ChallengeDetail /> },
      { path: 'recorder/corrections/:correctionId', element: <CorrectionDetail /> },
      ...PLACEHOLDERS.map((item) => ({ path: item.path.slice(1), element: <PlaceholderScreen item={item} /> })),
      { path: 'records/:collection', element: <SourceRecords /> },
      { path: '*', element: <NotFound /> },
    ],
  },
];

/**
 * Starts the SSE notification stream in live mode. Every event or resync just
 * invalidates projections (refetch); events never change capabilities.
 *
 * The backend serves no /events/stream route yet (src/api/operations.ts
 * classifies streamEvents as absent), so the stream is not opened: opening it
 * would fail every few seconds and tell the operator "reconnecting" forever.
 * The shell reports live updates as unavailable instead, and screens refresh
 * on navigation, on a command's own refetch, or when the operator reloads.
 */
function LiveUpdates() {
  const { client, fixtureId, invalidateAll, setStreamStatus, config, eventStream } = useApp();
  useEffect(() => {
    if (fixtureId) return; // no event stream in fixture mode
    if (!(eventStream ?? isLive('streamEvents'))) {
      setStreamStatus({ state: 'stopped', reason: 'unavailable' });
      return;
    }
    const stream = new EventStreamClient({
      open: liveStreamOpener(
        (cursor) => client.url('streamEvents', { query: { cursor } }),
        config.apiOrigin ? 'include' : 'same-origin',
        () => client.authHeaders(),
      ),
      onEvent: () => invalidateAll(),
      onResync: () => invalidateAll(),
      onStatus: setStreamStatus,
    });
    void stream.start();
    return () => stream.stop();
  }, [client, fixtureId, invalidateAll, setStreamStatus, config.apiOrigin, eventStream]);
  return null;
}

export function App({ services, initialEntries }: { services: AppServices; initialEntries?: string[] }) {
  const [router] = useState(() =>
    initialEntries
      ? createMemoryRouter(routes, { initialEntries, future: ROUTER_FUTURE })
      : createBrowserRouter(routes, { future: ROUTER_FUTURE }),
  );
  return (
    <AppProvider services={services}>
      <LiveUpdates />
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </AppProvider>
  );
}
