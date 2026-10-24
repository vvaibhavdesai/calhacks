import { useEffect, useState } from 'react';
import { RouterProvider, createBrowserRouter, createMemoryRouter, type RouteObject } from 'react-router-dom';
import { EventStreamClient, liveStreamOpener } from '../api/sse';
import { CaseDetail, CasesList } from '../screens/Cases';
import { CashScreen } from '../screens/Cash';
import { JournalDetail, JournalList } from '../screens/Journal';
import { InvoiceDetail, PayablesScreen } from '../screens/Payables';
import { PayoutDetail, PayoutsList } from '../screens/Payouts';
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
      ...PLACEHOLDERS.map((item) => ({ path: item.path.slice(1), element: <PlaceholderScreen item={item} /> })),
      { path: 'records/:collection', element: <SourceRecords /> },
      { path: '*', element: <NotFound /> },
    ],
  },
];

/**
 * Starts the SSE notification stream in live mode. Every event or resync just
 * invalidates projections (refetch); events never change capabilities.
 */
function LiveUpdates() {
  const { client, fixtureId, invalidateAll, setStreamStatus, config } = useApp();
  useEffect(() => {
    if (fixtureId) return; // no event stream in fixture mode
    const stream = new EventStreamClient({
      open: liveStreamOpener(
        (cursor) => client.url('streamEvents', { query: { cursor } }),
        config.apiOrigin ? 'include' : 'same-origin',
      ),
      onEvent: () => invalidateAll(),
      onResync: () => invalidateAll(),
      onStatus: setStreamStatus,
    });
    void stream.start();
    return () => stream.stop();
  }, [client, fixtureId, invalidateAll, setStreamStatus, config.apiOrigin]);
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
