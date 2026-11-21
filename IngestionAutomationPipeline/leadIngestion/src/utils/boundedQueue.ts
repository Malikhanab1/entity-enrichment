/**
 * Minimal bounded-concurrency task queue.
 *
 * Why not just fire every request at Ollama as it comes in? A local model
 * on a CPU-only box processes one inference at a time in practice — piling
 * concurrent requests on top of it doesn't parallelize, it just makes every
 * request slower and eventually times out under load. This queue:
 *   - runs at most `concurrency` jobs at once (default 1 — match it to how
 *     many inferences your hardware can actually run in parallel)
 *   - caps how many jobs can be waiting (`maxQueueSize`) so a burst of
 *     traffic fails fast (QueueFullError) instead of building an
 *     ever-growing backlog and OOMing or making every caller time out
 *   - queue depth is inspectable, so callers can log/alert on backpressure
 */
export class QueueFullError extends Error {
  constructor() {
    super("Ollama request queue is full");
    this.name = "QueueFullError";
  }
}

interface QueueItem<T> {
  task: () => Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

export class BoundedQueue {
  private readonly concurrency: number;
  private readonly maxQueueSize: number;
  private active = 0;
  private queue: QueueItem<any>[] = [];

  constructor(concurrency: number, maxQueueSize: number) {
    this.concurrency = Math.max(1, concurrency);
    this.maxQueueSize = Math.max(0, maxQueueSize);
  }

  get pending(): number {
    return this.queue.length;
  }

  get running(): number {
    return this.active;
  }

  /**
   * Enqueues a task. Rejects immediately with QueueFullError if the backlog
   * is already at capacity — callers should turn that into a 429.
   */
  add<T>(task: () => Promise<T>): Promise<T> {
    if (this.queue.length >= this.maxQueueSize) {
      return Promise.reject(new QueueFullError());
    }

    return new Promise<T>((resolve, reject) => {
      this.queue.push({ task, resolve, reject });
      this.drain();
    });
  }

  private drain(): void {
    while (this.active < this.concurrency && this.queue.length > 0) {
      const item = this.queue.shift()!;
      this.active++;
      item
        .task()
        .then(item.resolve, item.reject)
        .finally(() => {
          this.active--;
          this.drain();
        });
    }
  }
}
