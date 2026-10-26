use pool_proto::common::{Factory, Opts, PoolApi};
use pool_proto::{dp::DpPool, own::OwnPool};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

const OPS: usize = 400_000;

async fn run<P: PoolApi>(tasks: usize) -> f64 {
    let f = Arc::new(Factory::default());
    f.no_reset.store(true, Ordering::Relaxed);
    let p = Arc::new(P::new(Opts { min_size: 10, max_size: 10, ..Opts::default() }, f));
    p.maintain().await;
    let t = Instant::now();
    let hs: Vec<_> = (0..tasks)
        .map(|_| {
            let p = p.clone();
            tokio::spawn(async move {
                for _ in 0..OPS / tasks {
                    drop(p.acquire().await.unwrap());
                }
            })
        })
        .collect();
    for h in hs {
        h.await.unwrap();
    }
    OPS as f64 / t.elapsed().as_secs_f64()
}

async fn median<P: PoolApi>(tasks: usize) -> f64 {
    let mut v = [run::<P>(tasks).await, run::<P>(tasks).await, run::<P>(tasks).await, run::<P>(tasks).await, run::<P>(tasks).await];
    v.sort_by(f64::total_cmp);
    v[2]
}

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() {
    for tasks in [1, 64] {
        let d = median::<DpPool>(tasks).await;
        let o = median::<OwnPool>(tasks).await;
        println!("tasks={tasks:>2}  deadpool={d:>10.0} ops/s ({:>5.0} ns/op)  own={o:>10.0} ops/s ({:>5.0} ns/op)  own/deadpool={:.2}", 1e9 / d, 1e9 / o, o / d);
    }
}
