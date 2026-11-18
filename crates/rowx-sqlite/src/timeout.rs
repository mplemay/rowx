use std::io;
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

// the slot only holds plain data, so a poisoned lock (a panic elsewhere) is still consistent
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[derive(Debug)]
struct Slot {
    // bumped by every `arm`, so a guard from an earlier statement can never disarm a later one
    generation: u64,
    deadline: Option<Instant>,
    fired: bool,
    shutdown: bool,
}

#[derive(Debug)]
struct Inner {
    slot: Mutex<Slot>,
    wake: Condvar,
    timeout: Duration,
}

/// The `statement_timeout` interrupt timer of one connection.
///
/// One timer thread per connection that sets a timeout, rather than a tokio sleep per statement: the
/// worker arms it with a lock and a notify, the thread owns no runtime handle, and the interrupt runs
/// where nothing else shares the thread. The thread exits when the timer drops.
#[derive(Debug)]
pub struct Timer {
    inner: Arc<Inner>,
}

impl Timer {
    /// `interrupt` runs on the timer thread, under the timer's lock, when an armed deadline passes.
    /// Holding the lock is what makes [`Armed::finish`] a barrier: once it returns, the arming it
    /// closes can no longer interrupt anything.
    ///
    /// # Errors
    /// The IO error when the thread cannot be spawned.
    pub fn new(timeout: Duration, interrupt: impl Fn() + Send + 'static) -> io::Result<Self> {
        let inner = Arc::new(Inner {
            slot: Mutex::new(Slot {
                generation: 0,
                deadline: None,
                fired: false,
                shutdown: false,
            }),
            wake: Condvar::new(),
            timeout,
        });
        let shared = Arc::clone(&inner);
        thread::Builder::new()
            .name("rowx-sqlite-timer".to_owned())
            .spawn(move || run(&shared, &interrupt))?;
        Ok(Self { inner })
    }

    /// Starts the clock for one statement (or one batch of a stream); dropping the guard stops it.
    #[must_use]
    pub fn arm(&self) -> Armed {
        let generation = {
            let mut slot = lock(&self.inner.slot);
            slot.generation = slot.generation.wrapping_add(1);
            slot.deadline = Instant::now().checked_add(self.inner.timeout);
            slot.fired = false;
            slot.generation
        };
        self.inner.wake.notify_all();
        Armed {
            inner: Arc::clone(&self.inner),
            generation,
        }
    }
}

impl Drop for Timer {
    fn drop(&mut self) {
        lock(&self.inner.slot).shutdown = true;
        self.inner.wake.notify_all();
    }
}

fn run(inner: &Inner, interrupt: &impl Fn()) {
    let mut slot = lock(&inner.slot);
    loop {
        if slot.shutdown {
            return;
        }
        let Some(deadline) = slot.deadline else {
            slot = inner
                .wake
                .wait(slot)
                .unwrap_or_else(PoisonError::into_inner);
            continue;
        };
        let now = Instant::now();
        if now >= deadline {
            slot.deadline = None;
            slot.fired = true;
            interrupt();
        } else {
            slot = inner
                .wake
                .wait_timeout(slot, deadline.saturating_duration_since(now))
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
    }
}

/// One running clock; it stops when dropped or finished.
#[derive(Debug)]
pub struct Armed {
    inner: Arc<Inner>,
    generation: u64,
}

impl Armed {
    fn disarm(&self) -> bool {
        let mut slot = lock(&self.inner.slot);
        if slot.generation != self.generation {
            return false;
        }
        slot.deadline = None;
        slot.fired
    }

    /// Stops the clock and reports whether it fired meanwhile.
    #[must_use]
    pub fn finish(self) -> bool {
        self.disarm()
    }
}

impl Drop for Armed {
    fn drop(&mut self) {
        self.disarm();
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::thread;
    use std::time::{Duration, Instant};

    use super::{Timer, lock};

    fn counter() -> (Arc<AtomicUsize>, impl Fn() + Send + 'static) {
        let fired = Arc::new(AtomicUsize::new(0));
        let seen = Arc::clone(&fired);
        (fired, move || {
            seen.fetch_add(1, Ordering::SeqCst);
        })
    }

    // polls an observable condition; the bound only guards against a hang
    fn wait_for(condition: impl Fn() -> bool) -> bool {
        let limit = Instant::now() + Duration::from_secs(10);
        while Instant::now() < limit {
            if condition() {
                return true;
            }
            thread::yield_now();
        }
        condition()
    }

    #[test]
    fn an_expired_deadline_interrupts_once() -> std::io::Result<()> {
        let (fired, interrupt) = counter();
        let timer = Timer::new(Duration::from_millis(1), interrupt)?;
        let armed = timer.arm();
        assert!(wait_for(|| fired.load(Ordering::SeqCst) == 1));
        assert!(armed.finish());
        assert_eq!(fired.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[test]
    fn finishing_before_the_deadline_never_interrupts() -> std::io::Result<()> {
        let (fired, interrupt) = counter();
        let timer = Timer::new(Duration::from_secs(3600), interrupt)?;
        let armed = timer.arm();
        assert!(!armed.finish());
        assert!(lock(&timer.inner.slot).deadline.is_none());
        assert_eq!(fired.load(Ordering::SeqCst), 0);
        Ok(())
    }

    #[test]
    fn an_older_guard_cannot_disarm_a_later_statement() -> std::io::Result<()> {
        let (_, interrupt) = counter();
        let timer = Timer::new(Duration::from_secs(3600), interrupt)?;
        let first = timer.arm();
        let second = timer.arm();
        drop(first);
        assert!(lock(&timer.inner.slot).deadline.is_some());
        drop(second);
        assert!(lock(&timer.inner.slot).deadline.is_none());
        Ok(())
    }

    #[test]
    fn an_older_guard_does_not_report_a_later_fire() -> std::io::Result<()> {
        let (fired, interrupt) = counter();
        let timer = Timer::new(Duration::from_millis(1), interrupt)?;
        let first = timer.arm();
        assert!(wait_for(|| fired.load(Ordering::SeqCst) == 1));
        let second = timer.arm();
        assert!(!first.finish());
        // the later arming is untouched by the earlier guard and fires on its own
        assert!(wait_for(|| fired.load(Ordering::SeqCst) == 2));
        assert!(second.finish());
        Ok(())
    }

    #[test]
    fn dropping_the_timer_stops_its_thread() -> std::io::Result<()> {
        let (fired, interrupt) = counter();
        let timer = Timer::new(Duration::from_secs(3600), interrupt)?;
        let armed = timer.arm();
        let inner = Arc::clone(&timer.inner);
        drop(timer);
        assert!(lock(&inner.slot).shutdown);
        drop(armed);
        assert_eq!(fired.load(Ordering::SeqCst), 0);
        Ok(())
    }
}
