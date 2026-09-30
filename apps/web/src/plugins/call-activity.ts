import manifest from '../../../../plugins/extensions/call-activity/manifest.json';
import {renderers} from './renderers';
import latestManifest from '../../../../plugins/extensions/call-activity-1.1.0/manifest.json';
import {renderers as latestRenderers} from './renderers-1.1';
import type {BrowserPlugin, PluginManifest} from './host/contracts';

/** Adapter for the immutable official 1.0.0 implementation; its source digest stays unchanged. */
export const callActivity: BrowserPlugin = {
  // JSON literals are widened by TypeScript; backend package tests validate this same manifest.
  manifest: manifest as unknown as PluginManifest,
  requires: ['hostVersion'],
  activate(context) {
    context.registerView('grid', renderers['trace.grid']);
    context.registerView('list', renderers['trace.list']);
  },
};

export const callActivityLatest: BrowserPlugin = {
  manifest: latestManifest as unknown as PluginManifest,
  requires: ['hostVersion'],
  activate(context) {
    context.registerView('grid', latestRenderers['trace.grid']);
    context.registerView('list', latestRenderers['trace.list']);
  },
};
