import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { Brand } from '../components/Brand';
import { FixtureBanner } from '../components/FixtureBanner';
import { SignIn } from '../components/SignIn';
import type { StreamStatus } from '../api/sse';
import { useApp } from './AppContext';
import { VISIBLE_NAV } from './navigation';

function streamLabel(s: StreamStatus, fixture: boolean): string {
  if (fixture) return 'Live updates off (fixture mode)';
  switch (s.state) {
    case 'open':
      return 'Live updates on';
    case 'connecting':
      return 'Connecting live updates…';
    case 'reconnecting':
      return `Live updates reconnecting (retry in ${Math.ceil(s.delayMs / 1000)} s)`;
    case 'stopped':
      return s.reason === 'unavailable'
        ? 'Live updates unavailable (the API has no event stream)'
        : s.reason === 'unauthenticated'
        ? 'Live updates stopped: sign in required'
        : s.reason === 'forbidden'
          ? 'Live updates not permitted'
          : 'Live updates stopped';
    default:
      return 'Live updates idle';
  }
}

export function Layout() {
  const { fixtureId, capabilities, streamStatus } = useApp();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  const lastPath = useRef(location.pathname);

  // Close the mobile menu and move focus to the new page on navigation.
  useEffect(() => {
    setMenuOpen(false);
    if (lastPath.current === location.pathname) return; // initial load (and StrictMode re-run)
    lastPath.current = location.pathname;
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo?.(0, 0);
  }, [location.pathname]);

  const actor =
    capabilities.status === 'ready' && capabilities.session
      ? capabilities.session.display_name ?? capabilities.session.actor_id
      : capabilities.status === 'loading'
        ? 'Checking session…'
        : capabilities.status === 'unauthenticated'
          ? 'Not signed in'
          : 'Session unavailable';

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      {fixtureId ? <FixtureBanner fixtureId={fixtureId} /> : null}
      <div className="shell-body">
        <aside className="sidebar" aria-label="Sidebar">
          <div className="sidebar-top">
            <Brand />
            <button
              type="button"
              className="menu-toggle button button-quiet"
              aria-expanded={menuOpen}
              aria-controls="primary-nav"
              onClick={() => setMenuOpen((o) => !o)}
            >
              {menuOpen ? 'Close menu' : 'Menu'}
            </button>
          </div>
          <nav id="primary-nav" aria-label="Primary" className={`nav${menuOpen ? ' is-open' : ''}`}>
            {VISIBLE_NAV.map((group) => (
              <div className="nav-group" key={group.label}>
                <p className="nav-group-label" id={`nav-${group.label}`}>
                  {group.label}
                </p>
                <ul aria-labelledby={`nav-${group.label}`}>
                  {group.items.map((item) => (
                    <li key={item.path}>
                      <NavLink
                        to={item.path}
                        end={item.path === '/'}
                        className={({ isActive }) => `nav-link${isActive ? ' is-active' : ''}`}
                      >
                        {item.label}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
          <div className="sidebar-foot">
            <p className="sidebar-meta">{actor}</p>
            <p className="sidebar-meta" role="status" aria-live="polite">
              {streamLabel(streamStatus, !!fixtureId)}
            </p>
          </div>
        </aside>
        <main id="main" ref={mainRef} tabIndex={-1} className="work-area">
          {/* Every read 401s without a token, so the shell offers the form
              above the page; the panels keep showing their own sign-in state. */}
          {capabilities.status === 'unauthenticated' ? <SignIn /> : null}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
