import type { CSSProperties } from 'react';

type Name = 'home' | 'grid' | 'capture' | 'upload' | 'plus' | 'arrow' | 'book' | 'search' | 'file' | 'copy' | 'check' | 'chevron' | 'spark' | 'pulse' | 'plug' | 'chat' | 'compare';
const paths: Record<Name, string> = {
  home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z',
  grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  capture: 'M8 3H4a1 1 0 0 0-1 1v4 M16 3h4a1 1 0 0 1 1 1v4 M21 16v4a1 1 0 0 1-1 1h-4 M8 21H4a1 1 0 0 1-1-1v-4 M8 12h8 M12 8v8',
  upload: 'M12 16V3 m-5 5 5-5 5 5 M3 15v5a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-5',
  plus: 'M12 5v14 M5 12h14',
  arrow: 'M4 12h16 m-6-6 6 6-6 6',
  book: 'M12 5C8 2 4 3 3 4v16c2-2 6-2 9 0 3-2 7-2 9 0V4c-1-1-5-2-9 1v15',
  search: 'M20 20l-5-5 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
  file: 'M14 2H5a1 1 0 0 0-1 1v18h16V8l-6-6v6h6 M8 13h8 M8 17h5',
  copy: 'M8 8h12v13H8z M16 8V3H3v13h5',
  check: 'm5 12 4 4L19 6',
  chevron: 'm9 5 7 7-7 7',
  spark: 'M12 3v4m0 10v4M3 12h4m10 0h4M5.6 5.6l2.8 2.8m7.2 7.2 2.8 2.8m0-12.8-2.8 2.8m-7.2 7.2-2.8 2.8',
  pulse: 'M3 12h4l2.2-6 4.2 12 2.2-6H21',
  plug: 'M8 3v5m8-5v5 M6 8h12v2a6 6 0 0 1-6 6v5 M9 21h6',
  chat: 'M4 4h16v12H9l-5 4z M8 8h8 M8 12h5',
  compare: 'M6 4v16 M18 4v16 M3 8l3-4 3 4 M15 16l3 4 3-4 M6 12h12',
};
export function WorkspaceIcon({ name, size = 18, style }: { name: Name; size?: number; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}><path d={paths[name]} /></svg>;
}
