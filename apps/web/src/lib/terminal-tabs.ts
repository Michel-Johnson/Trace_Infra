export type TerminalTarget = { sessionId: string | null; nonce: string };
export type TerminalTab = { id: string; target: TerminalTarget; importSessionId: string | null };
export type TerminalTabs = { items: TerminalTab[]; activeId: string };

const newNonce = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

export function createTerminalTab(sessionId: string | null = null, importSessionId: string | null = null): TerminalTab {
  const id = newNonce();
  return { id, target: { sessionId, nonce: id }, importSessionId };
}

export function openTerminalTab(current: TerminalTabs, sessionId: string | null = null,
  importSessionId: string | null = null): TerminalTabs {
  const existing = (sessionId || importSessionId) && current.items.find(tab =>
    importSessionId ? tab.importSessionId === importSessionId
      : !tab.importSessionId && tab.target.sessionId === sessionId);
  if (existing) return existing.id === current.activeId ? current : { ...current, activeId: existing.id };
  const tab = createTerminalTab(sessionId, importSessionId);
  return { items: [...current.items, tab], activeId: tab.id };
}
