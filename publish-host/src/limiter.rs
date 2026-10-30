//! Per-client token buckets. A client is an IP address (an IPv6 /64, since one subscriber
//! owns the whole prefix). Both reads and writes get one; writes are the tight one.

use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::Mutex;

use crate::budget::client_key;

pub struct Limiter {
    burst: f64,
    per_ms: f64,
    buckets: Mutex<HashMap<IpAddr, (f64, u64)>>,
}

impl Limiter {
    /// `per_minute == 0` means unlimited.
    pub fn new(burst: u32, per_minute: u32) -> Limiter {
        Limiter {
            burst: f64::from(burst.max(1)),
            per_ms: if per_minute == 0 {
                f64::INFINITY
            } else {
                f64::from(per_minute) / 60_000.0
            },
            buckets: Mutex::new(HashMap::new()),
        }
    }

    /// Takes a token, or says how many seconds until one is back.
    pub fn take(&self, ip: IpAddr, now_ms: u64) -> Result<(), u64> {
        if self.per_ms.is_infinite() {
            return Ok(());
        }
        let mut map = self.buckets.lock().unwrap_or_else(|e| e.into_inner());
        let (tokens, last) = map.entry(client_key(ip)).or_insert((self.burst, now_ms));
        let elapsed = now_ms.saturating_sub(*last) as f64;
        *tokens = (*tokens + elapsed * self.per_ms).min(self.burst);
        *last = now_ms;
        if *tokens >= 1.0 {
            *tokens -= 1.0;
            Ok(())
        } else {
            Err((((1.0 - *tokens) / self.per_ms) / 1000.0).ceil().max(1.0) as u64)
        }
    }

    /// Forgets clients whose bucket has refilled: they are indistinguishable from new ones.
    pub fn forget_idle(&self, now_ms: u64) {
        let mut map = self.buckets.lock().unwrap_or_else(|e| e.into_inner());
        map.retain(|_, (tokens, last)| {
            *tokens + now_ms.saturating_sub(*last) as f64 * self.per_ms < self.burst
        });
    }
}
