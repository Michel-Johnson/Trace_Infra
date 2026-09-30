export type TerminalSessionCard = {
  uid: string;
  badge: string;
  directory: string;
  prompt: string;
  active: boolean;
};

function brief(value: string | null | undefined, limit: number): string {
  const normalized = (value || '').replace(/\s+/g, ' ').trim();
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}…` : normalized;
}

export function sessionCard(uid: string, badge: string | null | undefined,
  directory: string | null | undefined, prompt: string | null | undefined,
  active: boolean): TerminalSessionCard {
  return {
    uid,
    badge: brief(badge, 24) || 'idle',
    directory: brief(directory, 56) || 'workspace',
    prompt: brief(prompt, 96) || '新会话',
    active,
  };
}

export function readTerminalSessionCards(doc: Document): TerminalSessionCard[] {
  return Array.from(doc.querySelectorAll<HTMLElement>('[data-testid="cockpit-row"][data-uid]'))
    .map(row => sessionCard(
      row.dataset.uid || '',
      row.querySelector('[data-testid="cockpit-badge"]')?.textContent,
      row.querySelector('[data-testid="cockpit-dir"]')?.textContent,
      row.querySelector('[data-testid="cockpit-memo"]')?.textContent
        || row.querySelector('[data-testid="cockpit-line"]')?.getAttribute('title'),
      row.classList.contains('rounded-r-none'),
    ));
}
