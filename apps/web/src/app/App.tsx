import { useEffect, useState } from 'react';
import { RouterProvider, createBrowserRouter, createMemoryRouter, type RouteObject } from 'react-router-dom';
import { isLive } from '../api/operations';
import { CaseDetail, CasesList } from '../screens/Cases';
import { CashScreen } from '../screens/Cash';
import { JournalDetail, JournalList } from '../screens/Journal';
import { InvoiceDetail, PayablesScreen } from '../screens/Payables';
import { AnalystAssistant } from '../screens/AnalystAssistant';
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
      { path: 'analyst-assistant', element: <AnalystAssistant /> },
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
 * Keeps the open screen current in live mode.
 *
 * The nightly close runs in a worker, so a vendor can go from held to paid
 * with no request from this browser. Rather than leave the operator reloading
 * to find out, this polls the event log's head position -- one cheap read --
 * and invalidates projections when it moves. Events only ever trigger a
 * refetch; they never change capabilities.
 *
 * Polling, not a stream: the backend serves no /events/stream route, and a
 * poll of an endpoint that already exists needs nothing new on the server. The
 * cost is that an update lands within POLL_MS rather than immediately, which
 * is the right trade for a back-office view. `src/api/sse.ts` stays as the
 * client half of a stream if one is served later.
 */
const LIVE_POLL_MS = 5000;

function LiveUpdates() {
  const { client, fixtureId, invalidateAll, setStreamStatus, eventStream } = useApp();
  useEffect(() => {
    if (fixtureId) return; // no live updates against fixtures
    if (!(eventStream ?? isLive('listEvents'))) {
      setStreamStatus({ state: 'stopped', reason: 'unavailable' });
      return;
    }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Only the first reading establishes a baseline; it must not trigger a
    // refetch of screens that have just loaded.
    let lastHead: number | null = null;

    const tick = async () => {
      if (stopped) return;
      try {
        const page = (await client.list('listEvents', { query: { limit: 1 } })) as unknown as {
          head_sequence?: number;
        };
        const head = typeof page.head_sequence === 'number' ? page.head_sequence : null;
        if (!stopped) {
          setStreamStatus({ state: 'open' });
          if (head !== null && lastHead !== null && head !== lastHead) invalidateAll();
          if (head !== null) lastHead = head;
        }
      } catch {
        // A failed poll is not worth interrupting the operator over, and not
        // worth flapping the indicator for: the data on screen is still
        // whatever the server last served, and the next tick retries in
        // LIVE_POLL_MS. The status is left as it was.
      }
      if (!stopped) timer = setTimeout(() => void tick(), LIVE_POLL_MS);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [client, fixtureId, invalidateAll, setStreamStatus, eventStream]);
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
