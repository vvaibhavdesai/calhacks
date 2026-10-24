import { Link } from 'react-router-dom';
import { useApp } from '../app/AppContext';
import { BusinessDate } from '../components/Formatted';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, Loading } from '../components/QueryState';
import { useApiQuery } from '../hooks/useApiQuery';

export function Overview() {
  const { client } = useApp();
  const festival = useApiQuery('festival', (signal) => client.get('getFestival', { signal }));

  return (
    <>
      <PageHeader title="Overview" />
      <section className="panel" aria-labelledby="festival-heading">
        <h2 id="festival-heading" className="panel-title">
          Festival
        </h2>
        {festival.status === 'loading' ? <Loading label="Loading festival" /> : null}
        {festival.status === 'error' ? <ErrorState error={festival.error} onRetry={festival.refetch} /> : null}
        {festival.status === 'success' ? (
          <dl className="facts">
            <div>
              <dt>Name</dt>
              <dd>{festival.data.name}</dd>
            </div>
            {festival.data.organizer_name ? (
              <div>
                <dt>Organizer</dt>
                <dd>{festival.data.organizer_name}</dd>
              </div>
            ) : null}
            <div>
              <dt>Dates</dt>
              <dd>
                <BusinessDate value={festival.data.start_date} /> – <BusinessDate value={festival.data.end_date} />
              </dd>
            </div>
            <div>
              <dt>Business time zone</dt>
              <dd>{festival.data.timezone}</dd>
            </div>
            <div>
              <dt>Currency</dt>
              <dd>{festival.data.currency}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>
                <span className="badge">{festival.data.status}</span> <span className="muted">v{festival.data.version}</span>
              </dd>
            </div>
          </dl>
        ) : null}
      </section>
      <section className="panel placeholder" aria-labelledby="pipeline-heading">
        <h2 id="pipeline-heading" className="panel-title">
          Close-run pipeline: not yet implemented
        </h2>
        <p>
          The live vendor pipeline, holds and payout readiness arrive in B04 and will show server-calculated values only.
        </p>
        <p>
          Imported inputs can be inspected under <Link to="/records/vendors">Source records</Link>.
        </p>
      </section>
    </>
  );
}
