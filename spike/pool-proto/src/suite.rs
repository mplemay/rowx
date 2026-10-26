// The #75 requirement checklist, run against both prototypes (time is paused, so nothing depends on timing).
use crate::common::{Err, Factory, Opts, PoolApi};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::time::{Duration, Instant, sleep};

async fn settle() {
    sleep(Duration::from_millis(50)).await; // lets release tasks finish (reset takes 1ms)
}

fn mk<P: PoolApi>(o: Opts) -> (P, Arc<Factory>) {
    let f = Arc::new(Factory::default());
    (P::new(o, f.clone()), f)
}

pub async fn lifo_idle_reuse<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts::default());
    let (a, b) = (p.acquire().await.unwrap(), p.acquire().await.unwrap());
    let (ida, idb) = (a.id, b.id);
    drop(a);
    settle().await;
    drop(b);
    settle().await;
    assert_eq!(p.acquire().await.unwrap().id, idb, "last released is reused first");
    assert_ne!(ida, idb);
}

pub async fn fifo_waiters<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_size: 1, ..Opts::default() });
    let p = Arc::new(p);
    let held = p.acquire().await.unwrap();
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    for n in 0..3 {
        let (p, tx) = (p.clone(), tx.clone());
        tokio::spawn(async move {
            let l = p.acquire().await.unwrap();
            tx.send(n).unwrap();
            drop(l);
        });
        sleep(Duration::from_millis(1)).await; // enqueue in order
    }
    drop(held);
    let got = [rx.recv().await.unwrap(), rx.recv().await.unwrap(), rx.recv().await.unwrap()];
    assert_eq!(got, [0, 1, 2]);
}

pub async fn acquire_timeout<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_size: 1, ..Opts::default() });
    let _held = p.acquire().await.unwrap();
    let t = Instant::now();
    assert_eq!(p.acquire().await.err(), Some(Err::Timeout));
    assert!(t.elapsed() >= Duration::from_secs(5));
}

pub async fn max_lifetime<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts::default());
    let id = p.acquire().await.unwrap().id;
    settle().await;
    assert_eq!(p.acquire().await.unwrap().id, id);
    settle().await;
    sleep(Duration::from_secs(601)).await;
    assert_ne!(p.acquire().await.unwrap().id, id);
}

pub async fn max_queries<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_queries: Some(2), ..Opts::default() });
    let id = p.acquire().await.unwrap().id;
    settle().await;
    assert_eq!(p.acquire().await.unwrap().id, id); // second use
    settle().await;
    assert_ne!(p.acquire().await.unwrap().id, id); // retired after two
}

pub async fn expire_generation<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts::default());
    let id = p.acquire().await.unwrap().id;
    settle().await;
    p.expire();
    assert_ne!(p.acquire().await.unwrap().id, id);
}

pub async fn idle_reaping_above_min<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { min_size: 1, ..Opts::default() });
    let ls = [p.acquire().await.unwrap(), p.acquire().await.unwrap(), p.acquire().await.unwrap()];
    drop(ls);
    settle().await;
    assert_eq!(p.size(), 3);
    sleep(Duration::from_secs(61)).await;
    p.maintain().await;
    assert_eq!((p.size(), p.idle()), (1, 1));
}

pub async fn min_size_maintenance<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { min_size: 2, ..Opts::default() });
    p.maintain().await;
    assert_eq!((p.size(), p.idle()), (2, 2));
    let l = p.acquire().await.unwrap();
    let dead = l.id;
    drop(l);
    p.expire();
    settle().await;
    p.maintain().await; // replaces what expiry removed
    assert_eq!(p.size(), 2);
    let _ = dead;
}

pub async fn close_waits_for_checked_out<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_size: 1, ..Opts::default() });
    let p = Arc::new(p);
    let held = p.acquire().await.unwrap();
    let waiter = tokio::spawn({
        let p = p.clone();
        async move { p.acquire().await.err() }
    });
    sleep(Duration::from_millis(1)).await;
    let closer = tokio::spawn({
        let p = p.clone();
        async move { p.close().await }
    });
    sleep(Duration::from_secs(1)).await;
    assert_eq!(waiter.await.unwrap(), Some(Err::Closed), "waiters fail at once");
    assert!(!closer.is_finished(), "close waits for the lease");
    drop(held);
    closer.await.unwrap();
    assert_eq!(p.acquire().await.err(), Some(Err::Closed));
    assert_eq!(p.size(), 0);
}

pub async fn failed_reset_discards<P: PoolApi>() {
    let (p, f) = mk::<P>(Opts::default());
    let id = p.acquire().await.unwrap().id;
    settle().await;
    f.fail_reset.store(true, Ordering::Relaxed);
    drop(p.acquire().await.unwrap()); // reuses id, then its reset fails
    settle().await;
    f.fail_reset.store(false, Ordering::Relaxed);
    assert_eq!(p.size(), 0);
    assert_ne!(p.acquire().await.unwrap().id, id);
}

pub async fn release_waits_for_settling<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_size: 1, ..Opts::default() });
    let l = p.acquire().await.unwrap();
    let id = l.id;
    l.cancel_in_flight(Duration::from_millis(100));
    drop(l);
    let t = Instant::now();
    let again = p.acquire().await.unwrap();
    assert!(t.elapsed() >= Duration::from_millis(100), "capacity counts a settling connection");
    assert_eq!(again.id, id);
}

pub async fn drop_outside_runtime<P: PoolApi>() {
    let (p, _) = mk::<P>(Opts { max_size: 1, ..Opts::default() });
    let l = p.acquire().await.unwrap();
    std::thread::spawn(move || drop(l)).join().unwrap();
    settle().await;
    assert_eq!((p.size(), p.idle()), (1, 1));
    p.acquire().await.unwrap(); // capacity came back
}

macro_rules! one {
    ($p:ty, $($n:ident),*) => {$(
        #[tokio::test(start_paused = true)]
        async fn $n() {
            super::$n::<$p>().await
        }
    )*};
}

macro_rules! suite {
    ($m:ident, $p:ty) => {
        #[cfg(test)]
        mod $m {
            one!($p, lifo_idle_reuse, fifo_waiters, acquire_timeout, max_lifetime, max_queries, expire_generation,
                idle_reaping_above_min, min_size_maintenance, close_waits_for_checked_out, failed_reset_discards,
                release_waits_for_settling, drop_outside_runtime);
        }
    };
}
suite!(deadpool_proto, crate::dp::DpPool);
suite!(own_proto, crate::own::OwnPool);
