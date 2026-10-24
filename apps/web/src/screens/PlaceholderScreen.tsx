import type { NavItem } from '../app/navigation';
import { PageHeader } from '../components/PageHeader';

export function PlaceholderScreen({ item }: { item: NavItem }) {
  return (
    <>
      <PageHeader title={item.label} qualifier={item.qualifier} />
      <section className="panel placeholder" aria-labelledby="ph-status">
        <h2 id="ph-status" className="panel-title">
          Not yet implemented
        </h2>
        <p>{item.summary}</p>
        <p className="muted">
          Planned in lane B task {item.task}, once the matching lane A endpoints exist. This screen intentionally shows
          no sample figures.
        </p>
      </section>
    </>
  );
}
