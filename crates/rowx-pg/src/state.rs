use std::sync::Arc;
use std::sync::atomic::{AtomicU8, Ordering};

use tokio::sync::Notify;

use crate::error::Error;

const IDLE: u8 = 0;
const BUSY: u8 = 1;
const SETTLING: u8 = 2;
const BROKEN: u8 = 3;
const CLOSED: u8 = 4;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Busy,
    Settling,
    Broken,
    Closed,
}

#[derive(Debug, Default)]
pub struct State {
    phase: AtomicU8,
    settled: Notify,
}

impl State {
    /// Idle → Busy by compare-and-swap; the returned guard moves Busy → Idle when dropped.
    /// `None` means a cancelled operation is still settling, so the caller should use `claim_settled`.
    ///
    /// # Errors
    /// `Busy` when another operation holds the connection, `Closed` after `close()`,
    /// `Broken` after an IO failure.
    pub fn claim(self: &Arc<Self>) -> Result<Option<Claim>, Error> {
        match self
            .phase
            .compare_exchange(IDLE, BUSY, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => Ok(Some(Claim::new(self))),
            Err(BUSY) => Err(Error::Busy),
            Err(SETTLING) => Ok(None),
            Err(BROKEN) => Err(Error::Broken),
            Err(_) => Err(Error::Closed),
        }
    }

    /// Waits out `Settling`, then retries the claim.
    ///
    /// # Errors
    /// The same as `claim`, evaluated after the connection has settled.
    pub async fn claim_settled(self: &Arc<Self>) -> Result<Claim, Error> {
        loop {
            // registered before the claim so a settle between the two is not missed
            let notified = self.settled.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(claim) = self.claim()? {
                return Ok(claim);
            }
            notified.await;
        }
    }

    /// Busy → Settling, for a cancelled operation whose request is still on the wire.
    /// Returns `false` when the connection was not busy, for example because it was closed.
    pub fn settle(&self) -> bool {
        self.phase
            .compare_exchange(BUSY, SETTLING, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }

    /// Settling → Idle (or Broken after an IO error) and wakes every waiting call.
    pub fn settled(&self, ok: bool) {
        let next = if ok { IDLE } else { BROKEN };
        // a failed swap means the connection was closed meanwhile, which must stay closed
        let _ = self
            .phase
            .compare_exchange(SETTLING, next, Ordering::AcqRel, Ordering::Acquire);
        self.settled.notify_waiters();
    }

    /// Any live state → Broken; `Closed` is terminal and stays.
    pub fn mark_broken(&self) {
        let mut current = self.phase.load(Ordering::Acquire);
        while current != CLOSED && current != BROKEN {
            match self
                .phase
                .compare_exchange(current, BROKEN, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => break,
                Err(actual) => current = actual,
            }
        }
        self.settled.notify_waiters();
    }

    pub fn close(&self) {
        self.phase.store(CLOSED, Ordering::Release);
        self.settled.notify_waiters();
    }

    #[must_use]
    pub fn phase(&self) -> Phase {
        match self.phase.load(Ordering::Acquire) {
            IDLE => Phase::Idle,
            BUSY => Phase::Busy,
            SETTLING => Phase::Settling,
            BROKEN => Phase::Broken,
            _ => Phase::Closed,
        }
    }

    #[must_use]
    pub fn is_closed(&self) -> bool {
        self.phase() == Phase::Closed
    }

    #[must_use]
    pub fn is_busy(&self) -> bool {
        self.phase() == Phase::Busy
    }
}

#[derive(Debug)]
pub struct Claim {
    state: Arc<State>,
    release: bool,
}

impl Claim {
    fn new(state: &Arc<State>) -> Self {
        Self {
            state: Arc::clone(state),
            release: true,
        }
    }

    /// Drops the claim but leaves the connection Busy, for a cancelled operation whose canceller
    /// moves it Busy → Settling; releasing first would let another call claim it in between.
    pub fn hand_off(mut self) {
        self.release = false;
    }
}

impl Drop for Claim {
    fn drop(&mut self) {
        if !self.release {
            return;
        }
        // a failed swap means the state moved on (Settling, Broken or Closed), which must stay
        let _ = self
            .state
            .phase
            .compare_exchange(BUSY, IDLE, Ordering::AcqRel, Ordering::Acquire);
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn claimed(state: &Arc<State>) -> Claim {
        let swapped = state
            .phase
            .compare_exchange(IDLE, BUSY, Ordering::AcqRel, Ordering::Acquire);
        assert!(swapped.is_ok(), "state must be idle");
        Claim::new(state)
    }

    #[test]
    fn a_handed_off_claim_stays_busy_until_settled() {
        let state = Arc::new(State::default());
        claimed(&state).hand_off();
        assert_eq!(state.phase(), Phase::Busy);
        assert_eq!(state.claim().err(), Some(Error::Busy));
        assert!(state.settle());
        state.settled(true);
        assert_eq!(state.phase(), Phase::Idle);
    }

    #[test]
    fn idle_to_busy_and_back_on_drop() {
        let state = Arc::new(State::default());
        assert_eq!(state.phase(), Phase::Idle);
        let claim = claimed(&state);
        assert_eq!(state.phase(), Phase::Busy);
        drop(claim);
        assert_eq!(state.phase(), Phase::Idle);
    }

    #[test]
    fn busy_rejects_a_second_claim() {
        let state = Arc::new(State::default());
        let _claim = claimed(&state);
        assert_eq!(state.claim().err(), Some(Error::Busy));
        assert!(state.is_busy());
    }

    #[test]
    fn busy_to_settling_to_idle() {
        let state = Arc::new(State::default());
        let claim = claimed(&state);
        assert!(state.settle());
        assert_eq!(state.phase(), Phase::Settling);
        // dropping the claim must not turn Settling back into Idle
        drop(claim);
        assert_eq!(state.phase(), Phase::Settling);
        assert!(matches!(state.claim(), Ok(None)));
        state.settled(true);
        assert_eq!(state.phase(), Phase::Idle);
        assert!(state.claim().is_ok());
    }

    #[test]
    fn settling_to_broken_on_io_error() {
        let state = Arc::new(State::default());
        let _claim = claimed(&state);
        assert!(state.settle());
        state.settled(false);
        assert_eq!(state.phase(), Phase::Broken);
        assert_eq!(state.claim().err(), Some(Error::Broken));
    }

    #[test]
    fn settle_requires_busy() {
        let state = Arc::new(State::default());
        assert!(!state.settle());
        assert_eq!(state.phase(), Phase::Idle);
    }

    #[test]
    fn settled_outside_settling_changes_nothing() {
        let state = Arc::new(State::default());
        state.settled(false);
        assert_eq!(state.phase(), Phase::Idle);
        let _claim = claimed(&state);
        state.settled(true);
        assert_eq!(state.phase(), Phase::Busy);
    }

    #[test]
    fn broken_from_idle_and_busy() {
        let idle = Arc::new(State::default());
        idle.mark_broken();
        assert_eq!(idle.claim().err(), Some(Error::Broken));

        let busy = Arc::new(State::default());
        let claim = claimed(&busy);
        busy.mark_broken();
        drop(claim);
        assert_eq!(busy.phase(), Phase::Broken);
    }

    #[test]
    fn closed_is_terminal_from_every_state() {
        let state = Arc::new(State::default());
        let claim = claimed(&state);
        state.close();
        drop(claim);
        assert!(state.is_closed());
        assert_eq!(state.claim().err(), Some(Error::Closed));
        assert!(!state.settle());
        state.settled(true);
        state.mark_broken();
        assert!(state.is_closed());

        let settling = Arc::new(State::default());
        let _claim = claimed(&settling);
        assert!(settling.settle());
        settling.close();
        settling.settled(true);
        assert!(settling.is_closed());
    }

    #[tokio::test]
    async fn claim_in_settling_waits_for_the_notification() {
        let state = Arc::new(State::default());
        let claim = claimed(&state);
        assert!(state.settle());
        drop(claim);

        let waiter = {
            let state = Arc::clone(&state);
            tokio::spawn(async move { state.claim_settled().await.map(|_| ()) })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!waiter.is_finished());
        state.settled(true);
        assert_eq!(waiter.await.ok(), Some(Ok(())));
    }

    #[tokio::test]
    async fn waiters_fail_when_settling_ends_broken_or_closed() {
        for ok_then_close in [false, true] {
            let state = Arc::new(State::default());
            let claim = claimed(&state);
            assert!(state.settle());
            drop(claim);
            let waiter = {
                let state = Arc::clone(&state);
                tokio::spawn(async move { state.claim_settled().await.map(|_| ()) })
            };
            tokio::time::sleep(Duration::from_millis(20)).await;
            if ok_then_close {
                state.close();
            } else {
                state.settled(false);
            }
            let expected = if ok_then_close {
                Error::Closed
            } else {
                Error::Broken
            };
            assert_eq!(waiter.await.ok(), Some(Err(expected)));
        }
    }
}
