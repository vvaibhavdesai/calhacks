import { useEffect, type ReactNode } from 'react';

export function PageHeader({ title, qualifier, children }: { title: string; qualifier?: string | undefined; children?: ReactNode }) {
  useEffect(() => {
    document.title = `${title} · Encore`;
  }, [title]);
  return (
    <header className="page-header">
      <div>
        <h1 className="page-title">{title}</h1>
        {qualifier ? <p className="qualifier">{qualifier}</p> : null}
      </div>
      {children ? <div className="page-actions">{children}</div> : null}
    </header>
  );
}
