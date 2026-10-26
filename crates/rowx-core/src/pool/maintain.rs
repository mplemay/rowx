use std::sync::{Arc, Weak};
use std::time::Duration;

use tokio::time::Instant;

use super::{Inner, Manage, Parts, lock};

const MAX_BACKOFF: Duration = Duration::from_secs(30);

// holds only a `Weak`, so a pool that is dropped without `close()` stops its own task
pub(super) async fn run<M: Manage>(pool: Weak<Inner<M>>) {
    let Some(interval) = pool.upgrade().map(|inner| inner.options.maintain_interval) else {
        return;
    };
    let mut failures: u32 = 0;
    let mut delay = interval;
    loop {
        tokio::time::sleep(delay).await;
        let Some(inner) = pool.upgrade() else {
            return;
        };
        if inner.closed() {
            return;
        }
        failures = if inner.maintain().await {
            0
        } else {
            failures.saturating_add(1)
        };
        delay = interval
            .saturating_mul(1 << failures.min(6))
            .min(MAX_BACKOFF)
            .max(interval);
    }
}

impl<M: Manage> Inner<M> {
    // `false` when refilling failed, so the caller backs off
    async fn maintain(self: &Arc<Self>) -> bool {
        self.reap().await;
        self.fill().await.is_ok()
    }

    // closes idle connections that are retired or broken, and ones idle too long while above `min_size`
    async fn reap(&self) {
        let now = Instant::now();
        let stale: Vec<Parts<M>> = {
            let mut idle = lock(&self.idle);
            let mut size = self.size.load(std::sync::atomic::Ordering::Acquire);
            let (mut keep, mut stale) = (Vec::new(), Vec::new());
            for parts in idle.drain(..) {
                let dead = self.retired(&parts.meta, now) || self.manager.is_broken(&parts.conn);
                let idle_out = size > self.options.min_size
                    && self
                        .options
                        .idle_timeout
                        .is_some_and(|limit| now.duration_since(parts.meta.idle_since) >= limit);
                if dead || idle_out {
                    size = size.saturating_sub(1);
                    stale.push(parts);
                } else {
                    keep.push(parts);
                }
            }
            *idle = keep;
            stale
        };
        for parts in stale {
            self.discard(parts.conn).await;
        }
    }
}
