import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';

export function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <section className="panel">
        <p>There is no screen at this address.</p>
        <p>
          <Link to="/">Go to the overview</Link>
        </p>
      </section>
    </>
  );
}
