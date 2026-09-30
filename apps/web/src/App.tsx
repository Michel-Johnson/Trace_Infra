import { Navigate, useLocation } from 'react-router-dom';
import { AppShell } from './components/AppShell';

export function App() {
  const location = useLocation();
  const parts = location.pathname.split('/').filter(Boolean).map(decodeURIComponent);

  if (parts[0] === 'beta') {
    const pathname = '/' + parts.slice(1).map(encodeURIComponent).join('/');
    return <Navigate replace to={{ pathname, search: location.search }} />;
  }

  return <AppShell path={parts} />;
}
