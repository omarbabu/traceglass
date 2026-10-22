/**
 * Implicit parenting. On Node (and Deno/Bun) we use AsyncLocalStorage so spans opened inside a
 * callback — even across awaits and Promise.all — attach to the right parent automatically.
 * Elsewhere (browsers) implicit context is unavailable and parents are passed explicitly.
 */

export interface Store<T> {
  run<R>(value: T, fn: () => R): R;
  getStore(): T | undefined;
}

class NoopStore<T> implements Store<T> {
  run<R>(_value: T, fn: () => R): R {
    return fn();
  }
  getStore(): T | undefined {
    return undefined;
  }
}

let storeFactory: (<T>() => Store<T>) | null = null;

const ready: Promise<void> = (async () => {
  const isNode = typeof process !== "undefined" && !!(process as { versions?: { node?: string } }).versions?.node;
  if (!isNode) return;
  try {
    const spec = "node:async_hooks"; // computed so browser bundlers don't try to resolve it
    const mod = (await import(/* @vite-ignore */ spec)) as { AsyncLocalStorage: new <T>() => Store<T> };
    storeFactory = <T>() => new mod.AsyncLocalStorage<T>();
  } catch {
    /* no async context available */
  }
})();

/** A context slot that upgrades to AsyncLocalStorage once it has loaded. */
export class ContextSlot<T> {
  private impl: Store<T> | null = null;

  private get store(): Store<T> {
    if (!this.impl) {
      if (!storeFactory) return new NoopStore<T>();
      this.impl = storeFactory<T>();
    }
    return this.impl;
  }

  run<R>(value: T, fn: () => R): R {
    return this.store.run(value, fn);
  }

  get(): T | undefined {
    return this.impl?.getStore();
  }
}

export function contextReady(): Promise<void> {
  return ready;
}

export function hasAsyncContext(): boolean {
  return storeFactory !== null;
}
