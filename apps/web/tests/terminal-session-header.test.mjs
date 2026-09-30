import test from 'node:test';
import assert from 'node:assert/strict';
import { readTerminalSessionCards, sessionCard } from '../src/lib/terminal-session-header.ts';

test('active Mulmo roster row becomes a bounded header session card', () => {
  const row = {
    dataset: { uid: '7' },
    classList: { contains: value => value === 'rounded-r-none' },
    querySelector: selector => ({
      '[data-testid="cockpit-badge"]': { textContent: 'planning' },
      '[data-testid="cockpit-dir"]': { textContent: ' /workspace ' },
      '[data-testid="cockpit-line"]': { getAttribute: () => '  Analyze   benchmark\n traces  ' },
    })[selector] || null,
  };
  const document = { querySelectorAll: () => [row] };
  assert.deepEqual(readTerminalSessionCards(document), [{
    uid: '7', badge: 'planning', directory: '/workspace',
    prompt: 'Analyze benchmark traces', active: true,
  }]);
});

test('long prompts are clipped and empty sessions have a readable label', () => {
  const long = sessionCard('0', 'idle', '', 'a'.repeat(200), false);
  assert.equal(long.prompt.length, 96);
  assert.equal(long.prompt.at(-1), '…');
  assert.equal(sessionCard('1', '', '', '', false).prompt, '新会话');
});
