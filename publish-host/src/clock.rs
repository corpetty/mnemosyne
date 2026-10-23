//! Wall-clock time, replaceable by a hand-turned clock in tests.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Default)]
pub struct Clock(Option<Arc<AtomicU64>>);

impl Clock {
    pub fn system() -> Clock {
        Clock(None)
    }

    /// A clock that stands still at `secs` (unix seconds) until `advance`d.
    pub fn manual(secs: u64) -> Clock {
        Clock(Some(Arc::new(AtomicU64::new(secs * 1000))))
    }

    pub fn advance(&self, secs: u64) {
        if let Some(t) = &self.0 {
            t.fetch_add(secs * 1000, Ordering::SeqCst);
        }
    }

    pub fn now_ms(&self) -> u64 {
        match &self.0 {
            Some(t) => t.load(Ordering::SeqCst),
            None => SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| d.as_millis() as u64),
        }
    }

    pub fn now(&self) -> u64 {
        self.now_ms() / 1000
    }
}
