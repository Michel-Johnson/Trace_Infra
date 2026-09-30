import {Context, type Fiber} from '@deepseek-ai/cordis';
import {SlotCore, type PropsRenderSlots} from '@deepseek-ai/dsh-client-ui-slots';
import type {Extension} from '../../api/extensions';
import {
  BROWSER_HOST_VERSION, pluginKey, isRendererPlugin, type ActiveView, type BrowserPlugin,
  type BrowserPluginStatus, type HostSnapshot,
} from './contracts';

const canonical = (value: unknown) => JSON.stringify(value, (_, child) =>
  child && typeof child === 'object' && !Array.isArray(child)
    ? Object.fromEntries(Object.entries(child).sort(([a], [b]) => a.localeCompare(b))) : child);
type Mounted = {definition: BrowserPlugin; catalog: Extension; fiber?: Fiber; failure?: string};

/** Browser-local composition. Importing/activating views never invokes a computation API. */
export class BrowserPluginHost {
  private readonly context = new Context();
  private readonly slots = new SlotCore();
  private readonly mounted = new Map<string, Mounted>();
  private readonly disabled = new Set<string>();
  private readonly listeners = new Set<() => void>();
  private catalog: Extension[] = [];
  private snapshot: HostSnapshot = {revision: 0, selectedRenderer: null, views: [], plugins: []};
  // undefined uses the first compatible renderer; null explicitly disables UI plugins.
  private rendererPreference: string | null | undefined;
  private rendererOwner: string | null = null;
  private queue: Promise<void> = Promise.resolve();
  private disposed = false;
  private readonly releaseSlots: () => void;

  constructor(private readonly definitions: BrowserPlugin[], initialRenderer?: string | null) {
    this.rendererPreference = initialRenderer;
    const keys = definitions.map(d => pluginKey(d.manifest));
    if (new Set(keys).size !== keys.length) throw new Error('重复的浏览器插件版本');
    this.context.reflect.provide('hostVersion', BROWSER_HOST_VERSION);
    this.releaseSlots = this.slots.register({name: 'root', children: {
      'run.timeline': {kind: 'keyed', scope: 'root'},
    }}, (_props: PropsRenderSlots<'run.timeline'>) => null);
    this.context.on('internal/status', () => queueMicrotask(() => this.publish()));
  }

  getSnapshot = (): HostSnapshot => this.snapshot;
  subscribe = (listener: () => void) => {this.listeners.add(listener); return () => {this.listeners.delete(listener);};};

  reconcile(catalog: Extension[]): Promise<void> {
    if (this.disposed) return Promise.resolve();
    // Coalesce queued requests to the latest catalog. A running mutation still finishes before the next.
    this.catalog = structuredClone(catalog);
    this.queue = this.queue.catch(() => undefined).then(() => this.applyCatalog());
    return this.queue;
  }

  setEnabled(key: string, enabled: boolean): Promise<void> {
    const definition = this.definitions.find(d => pluginKey(d.manifest) === key);
    if (enabled && definition && isRendererPlugin(definition.manifest)) return this.selectRenderer(key);
    if (enabled) this.disabled.delete(key); else this.disabled.add(key);
    if (!enabled && key === this.rendererOwner) this.rendererPreference = null;
    return this.reconcile(this.catalog);
  }

  selectRenderer(key: string | null): Promise<void> {
    if (key !== null) {
      const definition = this.definitions.find(d => pluginKey(d.manifest) === key);
      const entry = this.catalog.find(e => pluginKey(e.manifest) === key);
      if (!definition || !entry || !this.canRender(definition, entry)) return Promise.reject(new Error('该 UI 插件当前不可用'));
      this.disabled.delete(key);
    }
    this.rendererPreference = key;
    return this.reconcile(this.catalog);
  }

  private compatible(definition: BrowserPlugin, entry: Extension): boolean {
    return entry.source === 'native' && canonical(definition.manifest) === canonical(entry.manifest);
  }

  private canRender(definition: BrowserPlugin, entry: Extension): boolean {
    return this.compatible(definition, entry) && entry.manifest.contributes.some(c =>
      c.kind === 'renderer' && c.implementation.host === 'browser' && entry.runtimes[c.id] === 'browser');
  }

  private async applyCatalog() {
    if (this.disposed) return;
    const desired = new Map(this.catalog.map(entry => [pluginKey(entry.manifest), entry]));
    const renderers = this.definitions.filter(d => {
      const key = pluginKey(d.manifest), entry = desired.get(key);
      return entry && !this.disabled.has(key) && this.canRender(d, entry);
    }).map(d => pluginKey(d.manifest));
    this.rendererOwner = this.rendererPreference === null ? null
      : renderers.includes(this.rendererPreference || '') ? this.rendererPreference!
      : renderers[0] || null;
    for (const [key, mounted] of this.mounted) {
      const target = desired.get(key);
      if (mounted.failure || this.disabled.has(key) || (isRendererPlugin(mounted.definition.manifest) && key !== this.rendererOwner)
          || !target || !this.compatible(mounted.definition, target)
          || canonical(target.runtimes) !== canonical(mounted.catalog.runtimes)) {
        // Withdraw the public view before awaiting effect cleanup.
        this.mounted.delete(key); this.publish();
        await mounted.fiber?.dispose();
      }
    }
    for (const definition of this.definitions) {
      if (this.disposed) return;
      const key = pluginKey(definition.manifest), target = desired.get(key);
      if (!target || this.disabled.has(key) || this.mounted.has(key) || !this.compatible(definition, target)) continue;
      if (isRendererPlugin(definition.manifest) && key !== this.rendererOwner) continue;
      if (!target.manifest.contributes.some(c => c.implementation.host === 'browser' && target.runtimes[c.id] === 'browser')) continue;
      const mounted: Mounted = {definition, catalog: target};
      this.mounted.set(key, mounted);
      try {
        const fiber = this.context.plugin({
          name: key, inject: definition.requires || [],
          apply: (ctx: Context) => definition.activate({
            get: name => ctx.get(name),
            provide: (name, service) => {ctx.reflect.provide(name, service);},
            effect: setup => {ctx.effect(setup);},
            registerView: (contributionId, component) => {
              if (key !== this.rendererOwner) throw new Error('只有选中的 UI 插件可以注册界面');
              const contribution = target.manifest.contributes.find(c => c.id === contributionId);
              if (!contribution || contribution.kind !== 'renderer' || contribution.trigger !== 'view'
                  || contribution.implementation.host !== 'browser' || !contribution.scopes.includes('run')
                  || !contribution.mounts?.includes('run.timeline')) throw new Error('视图不符合贡献点声明');
              if (target.runtimes[contributionId] !== 'browser') return;
              ctx.effect(() => this.slots.register({name: 'run.timeline', key: contribution.implementation.ref,
                registrant: key}, component));
            },
          }),
        });
        mounted.fiber = fiber;
        await fiber.await();
      } catch (error) {
        mounted.failure = error instanceof Error ? error.message : String(error);
        await mounted.fiber?.dispose();
        mounted.fiber = undefined;
      }
    }
    // A provider mounted later in the loop may activate a previously waiting consumer.
    for (const mounted of this.mounted.values()) {
      if (!mounted.fiber) continue;
      try {await mounted.fiber.await();}
      catch (error) {
        mounted.failure = error instanceof Error ? error.message : String(error);
        await mounted.fiber.dispose(); mounted.fiber = undefined;
      }
    }
    this.publish();
  }

  private publish() {
    if (this.disposed) return;
    const plugins: BrowserPluginStatus[] = [];
    const views: ActiveView[] = [];
    for (const entry of this.catalog) {
      if (!entry.manifest.contributes.some(c => c.implementation.host === 'browser')) continue;
      const key = pluginKey(entry.manifest), mounted = this.mounted.get(key);
      const renderer = isRendererPlugin(entry.manifest);
      const definition = this.definitions.find(d => pluginKey(d.manifest) === key);
      const selectable = !!definition && this.canRender(definition, entry);
      let state: BrowserPluginStatus['state'] = 'unavailable', reason = '当前前端未包含匹配的插件版本';
      if (this.disabled.has(key)) {state = 'disabled'; reason = '界面视图已在当前页面停用';}
      else if (renderer && selectable && key !== this.rendererOwner) {state = 'standby'; reason = '未选中；一次只加载一个 UI 插件';}
      else if (mounted && (!this.compatible(mounted.definition, entry)
          || canonical(entry.runtimes) !== canonical(mounted.catalog.runtimes))) {state = 'unavailable'; reason = '插件版本已变化，等待重新加载';}
      else if (mounted?.failure) {state = 'failed'; reason = mounted.failure;}
      // Cordis 4.0.2: ACTIVE=2; the dependency container can withdraw and reactivate a fiber.
      else if (mounted?.fiber?.state === 2) {state = 'active'; reason = '';}
      else if (mounted) {state = 'waiting'; reason = '等待所需服务';}
      plugins.push({key, state, reason, renderer, selectable});
      if (state !== 'active') continue;
      for (const record of this.slots.entriesOfSlot('run.timeline')) {
        if (record.registrant !== key) continue;
        const contribution = entry.manifest.contributes.find(c => c.kind === 'renderer' && c.implementation.ref === record.options.key);
        if (contribution) views.push({id: contribution.implementation.ref, title: contribution.title, owner: key,
          contributionId: contribution.id, component: record.component as ActiveView['component'], traceVersions: contribution.consumes});
      }
    }
    this.snapshot = {revision: this.snapshot.revision + 1, selectedRenderer: this.rendererOwner, views, plugins};
    for (const listener of this.listeners) listener();
  }

  async dispose(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.snapshot = {revision: this.snapshot.revision + 1, selectedRenderer: null, views: [], plugins: []};
    for (const listener of this.listeners) listener();
    await this.queue;
    await this.context.fiber.dispose();
    this.releaseSlots(); this.mounted.clear(); this.listeners.clear();
  }
}
