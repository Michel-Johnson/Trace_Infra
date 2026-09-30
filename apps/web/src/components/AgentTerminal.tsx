import { useEffect, useRef, useState } from 'react';
import { unifyAgentSessions, type AgentSessionEntry, type InfraSessionRecord, type TerminalSessionRecord } from '../lib/agent-session-catalog';
import { readTerminalSessionCards, type TerminalSessionCard } from '../lib/terminal-session-header';
import { createTerminalTab, openTerminalTab, type TerminalTab, type TerminalTabs, type TerminalTarget } from '../lib/terminal-tabs';
import type { TaskAgentTarget } from '../lib/task-agent-link';
import './AgentTerminal.css';

const widthKey = 'trace-hunter-agent-panel-width';
const fontSizeKey = 'terminalFontSize';
const defaultFontSize = 14;
const minFontSize = 8;
const maxFontSize = 32;
const workspace = '/var/lib/trace-hunter-terminal/workspace';

export function terminalUrl(target: TerminalTarget, importSessionId: string | null = null): string {
  const params = new URLSearchParams({ trace_hunter: '1', launch: target.nonce });
  if (importSessionId) {
    params.set('resume', importSessionId);
    params.set('cwd', `${workspace}/.trace-hunter-imports/${importSessionId}`);
  }
  else if (target.sessionId) params.set('resume', target.sessionId);
  return `/terminals?${params}`;
}

function clampWidth(width: number): number {
  const maximum = Math.max(280, window.innerWidth - 320);
  return Math.min(Math.max(width, Math.min(420, maximum)), maximum);
}

function clampFontSize(size: number): number {
  return Number.isFinite(size) ? Math.min(maxFontSize, Math.max(minFontSize, Math.round(size))) : defaultFontSize;
}

export function AgentTerminal({ onClose, initialSessionId, initialSessionKind, onSessionChange, visible = true }: {
  onClose: () => void;
  initialSessionId: string | null;
  initialSessionKind: TaskAgentTarget['kind'];
  onSessionChange: (id: string | null, kind?: TaskAgentTarget['kind']) => void;
  visible?: boolean;
}) {
  const [tabs, setTabs] = useState<TerminalTabs>(() => {
    const first = createTerminalTab(initialSessionKind === 'interactive' ? initialSessionId : null,
      initialSessionKind === 'native_import' ? initialSessionId : null);
    return { items: [first], activeId: first.id };
  });
  const [sessions, setSessions] = useState<AgentSessionEntry[]>([]);
  const [cardsByTab, setCardsByTab] = useState<Record<string, TerminalSessionCard[]>>({});
  const [historyError, setHistoryError] = useState(false);
  const [fontSize, setFontSize] = useState(() => {
    try { return clampFontSize(Number(localStorage.getItem(fontSizeKey)) || defaultFontSize); }
    catch { return defaultFontSize; }
  });
  const iframes = useRef(new Map<string, HTMLIFrameElement>());
  const [width, setWidth] = useState(() => {
    try { return clampWidth(Number(localStorage.getItem(widthKey)) || 760); }
    catch { return clampWidth(760); }
  });
  const [expanded, setExpanded] = useState(false);
  const [resizing, setResizing] = useState(false);
  const dragging = useRef(false);

  useEffect(() => {
    try { localStorage.setItem(widthKey, String(width)); } catch { /* storage may be unavailable */ }
  }, [width]);
  useEffect(() => {
    try { localStorage.setItem(fontSizeKey, String(fontSize)); } catch { /* storage may be unavailable */ }
  }, [fontSize]);
  useEffect(() => {
    const onResize = () => setWidth(current => clampWidth(current));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  useEffect(() => {
    if (!initialSessionId) return;
    setTabs(current => openTerminalTab(current,
      initialSessionKind === 'interactive' ? initialSessionId : null,
      initialSessionKind === 'native_import' ? initialSessionId : null));
  }, [initialSessionId, initialSessionKind]);
  useEffect(() => { void refreshSessions(); }, []);
  useEffect(() => {
    const sendFontSize = (frame: HTMLIFrameElement) => frame.contentWindow?.postMessage(
      { type: 'trace-hunter-terminal-font-size', size: fontSize }, window.location.origin);
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== window.location.origin || event.data?.type !== 'trace-hunter-terminal-ready') return;
      for (const frame of iframes.current.values()) {
        if (event.source === frame.contentWindow) { sendFontSize(frame); break; }
      }
    };
    window.addEventListener('message', onMessage);
    for (const frame of iframes.current.values()) sendFontSize(frame);
    return () => window.removeEventListener('message', onMessage);
  }, [fontSize, tabs.items]);
  useEffect(() => {
    const syncAll = () => { for (const tab of tabs.items) syncTerminalCards(tab.id); };
    const timer = window.setInterval(syncAll, 1000);
    syncAll();
    return () => window.clearInterval(timer);
  }, [tabs.items]);

  function syncTerminalCards(tabId: string) {
    let doc: Document | null = null;
    try { doc = iframes.current.get(tabId)?.contentDocument || null; } catch { /* Keep native roster if cross-origin. */ }
    if (!doc?.head) return;
    const cards = readTerminalSessionCards(doc);
    if (!cards.length) {
      setCardsByTab(current => current[tabId]?.length ? { ...current, [tabId]: [] } : current);
      return;
    }
    if (!doc.getElementById('trace-hunter-header-sessions')) {
      const style = doc.createElement('style');
      style.id = 'trace-hunter-header-sessions';
      style.textContent = '.stage.traceHunterEmbedded.zoomed.listmode [data-testid="cockpit"]{display:none!important}.stage.traceHunterEmbedded.zoomed.listmode .zoom-row{flex:1 1 auto;min-height:0}';
      doc.head.appendChild(style);
    }
    setCardsByTab(current => JSON.stringify(current[tabId]) === JSON.stringify(cards) ? current : { ...current, [tabId]: cards });
  }

  function focusTerminalCard(tab: TerminalTab, uid?: string) {
    setTabs(current => current.activeId === tab.id ? current : { ...current, activeId: tab.id });
    onSessionChange(tab.importSessionId || tab.target.sessionId,
      tab.importSessionId ? 'native_import' : 'interactive');
    if (!uid) return;
    let doc: Document | null = null;
    try { doc = iframes.current.get(tab.id)?.contentDocument || null; } catch { return; }
    const row = Array.from(doc?.querySelectorAll<HTMLElement>('[data-testid="cockpit-row"][data-uid]') || [])
      .find(item => item.dataset.uid === uid);
    row?.click();
    syncTerminalCards(tab.id);
  }

  async function refreshSessions() {
    const terminal = (async () => {
      const response = await fetch(`/api/sessions?cwd=${encodeURIComponent(workspace)}`, { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json() as Promise<{ sessions?: TerminalSessionRecord[] }>;
    })();
    const background = (async () => {
      const capability = await fetch('/api/v1/agent/capabilities', { credentials: 'same-origin' });
      if (!capability.ok) throw new Error(`HTTP ${capability.status}`);
      const response = await fetch('/api/v1/agent/sessions', { credentials: 'same-origin' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json() as Promise<{ items?: InfraSessionRecord[] }>;
    })();
    const [terminalResult, backgroundResult] = await Promise.allSettled([terminal, background]);
    setSessions(unifyAgentSessions(
      terminalResult.status === 'fulfilled' ? (terminalResult.value.sessions || []).slice(0, 50) : [],
      backgroundResult.status === 'fulfilled' ? (backgroundResult.value.items || []).slice(0, 50) : [],
    ));
    setHistoryError(terminalResult.status === 'rejected' && backgroundResult.status === 'rejected');
  }

  function showTerminal(sessionId: string | null) {
    setTabs(current => openTerminalTab(current, sessionId));
    onSessionChange(sessionId, 'interactive');
  }

  function chooseSession(key: string) {
    const item = sessions.find(row => row.key === key);
    if (!item) return;
    if (item.kind === 'interactive') showTerminal(item.sessionId);
    else if (item.kind === 'auto_import') {
      setTabs(current => openTerminalTab(current, null, item.sessionId));
      onSessionChange(item.sessionId, 'native_import');
    }
  }

  function resizeFromPointer(event: React.PointerEvent<HTMLDivElement>) {
    if (dragging.current) setWidth(clampWidth(window.innerWidth - event.clientX));
  }

  return <div className={`th-agent-terminal-overlay${resizing ? ' is-resizing' : ''}${visible ? '' : ' is-hidden'}`} role="complementary" aria-label="Trace Hunter Agent" aria-hidden={!visible} inert={!visible}>
    <section className={`th-agent-terminal-panel${expanded ? ' is-expanded' : ''}`} style={expanded ? undefined : { width }}>
    {!expanded ? <div className="th-agent-terminal-resizer" role="separator" tabIndex={0}
      aria-label="调整终端宽度" aria-orientation="vertical" aria-valuemin={Math.min(420, Math.max(280, window.innerWidth - 320))}
      aria-valuemax={Math.max(280, window.innerWidth - 320)} aria-valuenow={width}
      onPointerDown={event => { dragging.current = true; setResizing(true); event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={resizeFromPointer}
      onPointerUp={event => { dragging.current = false; setResizing(false); event.currentTarget.releasePointerCapture(event.pointerId); }}
      onPointerCancel={() => { dragging.current = false; setResizing(false); }}
      onKeyDown={event => {
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault(); setWidth(current => clampWidth(current + (event.key === 'ArrowLeft' ? 32 : -32)));
        }
      }} /> : null}
    <header className="th-agent-terminal-header">
      <div className="th-agent-terminal-session-strip" role="group" aria-label="当前终端会话">
        {tabs.items.flatMap(tab => {
          const cards = cardsByTab[tab.id];
          return cards?.length ? cards.map(card => <button type="button" key={`${tab.id}:${card.uid}`}
          className={`th-agent-terminal-session-card${tab.id === tabs.activeId && card.active ? ' is-active' : ''}`}
          aria-current={tab.id === tabs.activeId && card.active ? 'true' : undefined} title={card.prompt}
          onClick={() => focusTerminalCard(tab, card.uid)}>
          <span className="th-agent-terminal-session-main"><i className={`th-agent-terminal-session-dot${/wait|block/i.test(card.badge) ? ' is-waiting' : /plan|edit|run|work/i.test(card.badge) ? ' is-working' : ''}`}/><b>{card.badge}</b><img src="/claude-code-agent.svg" alt=""/><span>{card.directory}</span></span>
          <span className="th-agent-terminal-session-prompt">{card.prompt}</span>
        </button>) : [<button type="button" key={tab.id}
          className={`th-agent-terminal-session-card${tab.id === tabs.activeId ? ' is-active' : ''}`}
          aria-current={tab.id === tabs.activeId ? 'true' : undefined} onClick={() => focusTerminalCard(tab)}>
          <span className="th-agent-terminal-session-main"><i className="th-agent-terminal-session-dot"/><b>starting</b><img src="/claude-code-agent.svg" alt=""/><span>{tab.importSessionId || workspace}</span></span>
          <span className="th-agent-terminal-session-prompt">正在连接 Claude 会话…</span>
        </button>];
        })}
      </div>
      <div className="th-agent-terminal-controls"><button type="button" onClick={() => showTerminal(null)}>新会话</button>
        <select aria-label="Agent 会话历史" value="" onFocus={() => void refreshSessions()}
          onChange={event => { if (event.target.value) chooseSession(event.target.value); }}>
          <option value="">{historyError ? '历史会话暂不可用' : '选择历史会话…'}</option>
          {sessions.map(row => <option value={row.key} key={row.key}>
            {row.kind === 'interactive' ? '交互 · ' : ''}{row.label}{row.state ? ` · ${row.state}` : ''}
          </option>)}
        </select>
        <div className="th-agent-terminal-zoom" role="group" aria-label="终端内容缩放">
          <button type="button" aria-label="缩小终端内容" disabled={fontSize <= minFontSize} onClick={() => setFontSize(size => clampFontSize(size - 1))}>−</button>
          <button type="button" aria-label="重置终端内容缩放" title="重置为 100%" onClick={() => setFontSize(defaultFontSize)}>{Math.round(fontSize / defaultFontSize * 100)}%</button>
          <button type="button" aria-label="放大终端内容" disabled={fontSize >= maxFontSize} onClick={() => setFontSize(size => clampFontSize(size + 1))}>+</button>
        </div>
        <button type="button" onClick={() => setExpanded(value => !value)} aria-label={expanded ? '退出全屏' : '展开全屏'}>{expanded ? '收起' : '展开全屏'}</button>
        <button type="button" onClick={onClose} aria-label="关闭终端">×</button></div>
    </header>
    <div className="th-agent-terminal-frames">{tabs.items.map(tab => {
      const active = tab.id === tabs.activeId;
      return <div key={tab.id} className={`th-agent-terminal-frame${active ? ' is-active' : ''}`} aria-hidden={!active}>
        <iframe ref={frame => { if (frame) iframes.current.set(tab.id, frame); else iframes.current.delete(tab.id); }}
          onLoad={() => syncTerminalCards(tab.id)} title={`MulmoTerminal Claude 交互终端 ${tab.id}`}
          src={terminalUrl(tab.target, tab.importSessionId)} allow="clipboard-read; clipboard-write" />
      </div>;
    })}</div>
    </section>
  </div>;
}
