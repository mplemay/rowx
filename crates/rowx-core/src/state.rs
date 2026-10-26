use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};

use tokio::sync::Notify;

use crate::error::CoreError;

const IDLE: u64 = 0;
const BUSY: u64 = 1;
const SETTLING: u64 = 2;
const BROKEN: u64 = 3;
const CLOSED: u64 = 4;

// the phase and the lease share one word so that a claim checks both in a single compare-and-swap
const PHASE_MASK: u64 = 0xff;
const LEASE_SHIFT: u32 = 8;

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
    // low byte: phase; the rest: the current lease generation (0 for a connection outside a pool)
    word: AtomicU64,
    // conservative: set whenever the session may be inside a transaction, cleared by a reset
    maybe_in_txn: AtomicBool,
    // open helper transactions and savepoints on the session (the outermost transaction counts as 1)
    txn_depth: AtomicU32,
    settled: Notify,
}

impl State {
    // applies `next` to the phase while leaving the lease alone; `Err` carries the phase that refused
    fn update(&self, next: impl Fn(u64) -> Option<u64>) -> Result<u64, u64> {
        let mut cur = self.word.load(Ordering::Acquire);
        loop {
            let phase = cur & PHASE_MASK;
            let Some(to) = next(phase) else {
                return Err(phase);
            };
            match self.word.compare_exchange_weak(
                cur,
                (cur & !PHASE_MASK) | to,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(phase),
                Err(actual) => cur = actual,
            }
        }
    }

    /// Idle → Busy by compare-and-swap; the returned guard moves Busy → Idle when dropped.
    /// `None` means a cancelled operation is still settling, so the caller should use `claim_settled`.
    ///
    /// # Errors
    /// `Busy` when another operation holds the connection, `Closed` after `close()`,
    /// `Broken` after an IO failure.
    pub fn claim(self: &Arc<Self>) -> Result<Option<Claim>, CoreError> {
        self.claim_with(None)
    }

    /// `claim`, but only while `lease` is still the current lease; the check and Idle → Busy are one
    /// compare-and-swap, so a released lease can never claim a connection that was handed on.
    ///
    /// # Errors
    /// `StaleLease` when the lease was revoked, otherwise as `claim`.
    pub fn claim_leased(self: &Arc<Self>, lease: u64) -> Result<Option<Claim>, CoreError> {
        self.claim_with(Some(lease))
    }

    fn claim_with(self: &Arc<Self>, lease: Option<u64>) -> Result<Option<Claim>, CoreError> {
        let mut cur = self.word.load(Ordering::Acquire);
        loop {
            if lease.is_some_and(|lease| cur >> LEASE_SHIFT != lease) {
                return Err(CoreError::StaleLease);
            }
            match cur & PHASE_MASK {
                IDLE => {
                    match self.word.compare_exchange_weak(
                        cur,
                        (cur & !PHASE_MASK) | BUSY,
                        Ordering::AcqRel,
                        Ordering::Acquire,
                    ) {
                        Ok(_) => return Ok(Some(Claim::new(self))),
                        Err(actual) => cur = actual,
                    }
                }
                BUSY => return Err(CoreError::Busy),
                SETTLING => return Ok(None),
                BROKEN => return Err(CoreError::Broken),
                _ => return Err(CoreError::Closed),
            }
        }
    }

    /// Starts a new lease and returns it, which revokes the previous one.
    pub fn renew_lease(&self) -> u64 {
        (self.word.fetch_add(1 << LEASE_SHIFT, Ordering::AcqRel) >> LEASE_SHIFT).wrapping_add(1)
    }

    /// The current lease generation.
    #[must_use]
    pub fn lease(&self) -> u64 {
        self.word.load(Ordering::Acquire) >> LEASE_SHIFT
    }

    /// Waits out `Settling`, then retries the claim.
    ///
    /// # Errors
    /// The same as `claim`, evaluated after the connection has settled.
    pub async fn claim_settled(self: &Arc<Self>) -> Result<Claim, CoreError> {
        self.claim_settled_with(None).await
    }

    /// `claim_settled` for a leased connection.
    ///
    /// # Errors
    /// `StaleLease` when the lease was revoked, otherwise as `claim_settled`.
    pub async fn claim_settled_leased(self: &Arc<Self>, lease: u64) -> Result<Claim, CoreError> {
        self.claim_settled_with(Some(lease)).await
    }

    async fn claim_settled_with(self: &Arc<Self>, lease: Option<u64>) -> Result<Claim, CoreError> {
        loop {
            // registered before the claim so a settle between the two is not missed
            let notified = self.settled.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if let Some(claim) = self.claim_with(lease)? {
                return Ok(claim);
            }
            notified.await;
        }
    }

    /// Resolves once the connection is no longer `Settling`, without claiming it.
    ///
    /// # Errors
    /// `Broken` or `Closed` when the connection did not survive.
    pub async fn wait_settled(&self) -> Result<(), CoreError> {
        loop {
            let notified = self.settled.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            match self.phase() {
                Phase::Settling => {}
                Phase::Broken => return Err(CoreError::Broken),
                Phase::Closed => return Err(CoreError::Closed),
                Phase::Idle | Phase::Busy => return Ok(()),
            }
            notified.await;
        }
    }

    /// Resolves once nothing holds a claim any more (the phase is no longer `Busy`).
    pub async fn wait_unclaimed(&self) {
        loop {
            let notified = self.settled.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.phase() != Phase::Busy {
                return;
            }
            notified.await;
        }
    }

    /// Resolves once `close()` has run, so a task that waits on something else can give up.
    pub async fn wait_closed(&self) {
        loop {
            let notified = self.settled.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.is_closed() {
                return;
            }
            notified.await;
        }
    }

    /// Busy → Settling, for a cancelled operation whose request is still on the wire.
    /// Returns `false` when the connection was not busy, for example because it was closed.
    pub fn settle(&self) -> bool {
        let settling = self
            .update(|phase| (phase == BUSY).then_some(SETTLING))
            .is_ok();
        if settling {
            // a cancelled operation may have left a transaction open or aborted
            self.mark_maybe_in_txn();
        }
        settling
    }

    pub fn mark_maybe_in_txn(&self) {
        self.maybe_in_txn.store(true, Ordering::Release);
    }

    /// Clears the flag and the helper depth, which is what a reset or a finished transaction does.
    pub fn clear_maybe_in_txn(&self) {
        self.maybe_in_txn.store(false, Ordering::Release);
        self.txn_depth.store(0, Ordering::Release);
    }

    #[must_use]
    pub fn maybe_in_txn(&self) -> bool {
        self.txn_depth() > 0 || self.maybe_in_txn.load(Ordering::Acquire)
    }

    #[must_use]
    pub fn txn_depth(&self) -> u32 {
        self.txn_depth.load(Ordering::Acquire)
    }

    /// Records a transaction or savepoint that was opened; returns the new depth.
    pub fn enter_txn(&self) -> u32 {
        self.txn_depth.fetch_add(1, Ordering::AcqRel) + 1
    }

    /// Records a savepoint that was released; returns the new depth.
    pub fn leave_txn(&self) -> u32 {
        let mut cur = self.txn_depth.load(Ordering::Acquire);
        loop {
            let next = cur.saturating_sub(1);
            match self.txn_depth.compare_exchange_weak(
                cur,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return next,
                Err(actual) => cur = actual,
            }
        }
    }

    /// Settling → Idle (or Broken after an IO error) and wakes every waiting call.
    pub fn settled(&self, ok: bool) {
        let next = if ok { IDLE } else { BROKEN };
        // a failed swap means the connection was closed meanwhile, which must stay closed
        let _ = self.update(|phase| (phase == SETTLING).then_some(next));
        self.settled.notify_waiters();
    }

    /// Any live state → Broken; `Closed` is terminal and stays.
    pub fn mark_broken(&self) {
        let _ = self.update(|phase| (phase != CLOSED && phase != BROKEN).then_some(BROKEN));
        self.settled.notify_waiters();
    }

    pub fn close(&self) {
        let _ = self.update(|_| Some(CLOSED));
        self.settled.notify_waiters();
    }

    #[must_use]
    pub fn phase(&self) -> Phase {
        match self.word.load(Ordering::Acquire) & PHASE_MASK {
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
        if self
            .state
            .update(|phase| (phase == BUSY).then_some(IDLE))
            .is_ok()
        {
            // a release waiting for the operation to end
            self.state.settled.notify_waiters();
        }
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    fn claimed(state: &Arc<State>) -> Claim {
        let swapped = state.update(|phase| (phase == IDLE).then_some(BUSY));
        assert!(swapped.is_ok(), "state must be idle");
        Claim::new(state)
    }

    #[test]
    fn a_handed_off_claim_stays_busy_until_settled() {
        let state = Arc::new(State::default());
        claimed(&state).hand_off();
        assert_eq!(state.phase(), Phase::Busy);
        assert_eq!(state.claim().err(), Some(CoreError::Busy));
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
        assert_eq!(state.claim().err(), Some(CoreError::Busy));
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
        assert_eq!(state.claim().err(), Some(CoreError::Broken));
    }

    #[test]
    fn settling_marks_maybe_in_txn_until_cleared() {
        let state = Arc::new(State::default());
        assert!(!state.maybe_in_txn());
        let _claim = claimed(&state);
        assert!(state.settle());
        assert!(state.maybe_in_txn());
        state.clear_maybe_in_txn();
        assert!(!state.maybe_in_txn());
    }

    #[test]
    fn the_depth_feeds_maybe_in_txn() {
        let state = State::default();
        assert_eq!(state.enter_txn(), 1);
        assert_eq!(state.enter_txn(), 2);
        assert!(state.maybe_in_txn());
        assert_eq!(state.leave_txn(), 1);
        state.clear_maybe_in_txn();
        assert_eq!(state.txn_depth(), 0);
        assert!(!state.maybe_in_txn());
        assert_eq!(state.leave_txn(), 0);
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
        assert_eq!(idle.claim().err(), Some(CoreError::Broken));

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
        assert_eq!(state.claim().err(), Some(CoreError::Closed));
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
                CoreError::Closed
            } else {
                CoreError::Broken
            };
            assert_eq!(waiter.await.ok(), Some(Err(expected)));
        }
    }

    #[test]
    fn a_revoked_lease_cannot_claim() {
        let state = Arc::new(State::default());
        let lease = state.renew_lease();
        assert!(matches!(state.claim_leased(lease), Ok(Some(_))));
        assert_eq!(state.phase(), Phase::Idle);
        let next = state.renew_lease();
        assert_ne!(lease, next);
        assert_eq!(state.claim_leased(lease).err(), Some(CoreError::StaleLease));
        assert!(matches!(state.claim_leased(next), Ok(Some(_))));
    }

    #[test]
    fn the_lease_survives_phase_changes() {
        let state = Arc::new(State::default());
        let lease = state.renew_lease();
        let claim = claimed(&state);
        assert!(state.settle());
        drop(claim);
        state.settled(true);
        state.mark_broken();
        assert_eq!(state.lease(), lease);
    }

    #[tokio::test]
    async fn wait_settled_resolves_when_settling_ends() {
        let state = Arc::new(State::default());
        let _claim = claimed(&state);
        assert!(state.settle());
        let waiter = {
            let state = Arc::clone(&state);
            tokio::spawn(async move { state.wait_settled().await })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!waiter.is_finished());
        state.settled(false);
        assert_eq!(waiter.await.ok(), Some(Err(CoreError::Broken)));
    }

    #[tokio::test]
    async fn wait_unclaimed_resolves_when_the_claim_drops() {
        let state = Arc::new(State::default());
        let claim = claimed(&state);
        let waiter = {
            let state = Arc::clone(&state);
            tokio::spawn(async move { state.wait_unclaimed().await })
        };
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!waiter.is_finished());
        drop(claim);
        assert!(waiter.await.is_ok());
    }
}
