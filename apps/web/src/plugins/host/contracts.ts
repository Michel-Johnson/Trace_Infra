import type {ReactNode} from 'react';
import type {Bundle, Operation, Row} from '../../api/types';
import type {Extension} from '../../api/extensions';

export const BROWSER_HOST_VERSION = 'trace-hunter/browser-host/1.0' as const;
export type RunViewProps = {
  bundle: Bundle;
  rows: {row: Row; index: number}[];
  metric: 'tool_ms' | 'cycle_ms';
  colors: (operation: Operation) => string[];
  showLetters: boolean;
  inspect: (row: Row, index: number) => void;
};
export type RunView = (props: RunViewProps) => ReactNode;
export type PluginManifest = Extension['manifest'];

// Authors can augment this map when adding a typed service shared by browser plugins.
export interface BrowserServices { hostVersion: typeof BROWSER_HOST_VERSION }
export interface BrowserPluginContext {
  get<K extends keyof BrowserServices>(name: K): BrowserServices[K];
  provide<K extends keyof BrowserServices>(name: K, service: BrowserServices[K]): void;
  effect(setup: () => (() => void)): void;
  registerView(contributionId: string, component: RunView): void;
}
export interface BrowserPlugin {
  manifest: PluginManifest;
  requires?: (keyof BrowserServices)[];
  activate(context: BrowserPluginContext): void;
}
export type BrowserPluginState = 'active' | 'waiting' | 'failed' | 'disabled' | 'standby' | 'unavailable';
export type BrowserPluginStatus = {key: string; state: BrowserPluginState; reason: string; renderer: boolean; selectable: boolean};
export type ActiveView = {
  id: string; title: string; owner: string; contributionId: string; component: RunView;
  traceVersions: string[];
};
export type HostSnapshot = {
  revision: number;
  selectedRenderer: string | null;
  views: ActiveView[];
  plugins: BrowserPluginStatus[];
};
export const pluginKey = (manifest: Pick<PluginManifest, 'plugin_id' | 'version'>) =>
  JSON.stringify([manifest.plugin_id, manifest.version]);
export const isRendererPlugin = (manifest: PluginManifest) =>
  manifest.contributes.some(c => c.kind === 'renderer' && c.implementation.host === 'browser');

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface SlotMap {
    root: {kind: 'single'; scope: 'root'; owner: object};
    // Registry lifetime is application-wide; each render occurrence receives its own Run props.
    'run.timeline': {kind: 'keyed'; scope: 'root'; owner: RunViewProps};
  }
}
