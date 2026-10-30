//! A per-client budget of bytes written over a rolling window (24 hours in the host).
//!
//! The write rate limit (`limiter.rs`) bounds how often a client may write; this bounds how much,
//! so one client cannot fill the quota on an open host. Every upload counts what it sent:
//! publication manifests and blobs, room frames and room snapshots. A client is the same key as
//! in `limiter.rs` (an IP, an IPv6 /64).
//!
//! The window is kept in `BUCKETS` slots (a client's bytes added in the same slot are one
//! entry), so a client costs at most `BUCKETS + 1` small entries however many writes it makes.
//! A slot expires when its *end* is a whole window old: the budget is never more generous than
//! the window, and holds a byte up to one slot (15 minutes of a day) longer than a strict
//! per-write window would.
//!
//! Memory: entries exist only for clients that wrote something in the last window; `forget_idle`
//! (run by the sweeper) drops the rest, and `MAX_CLIENTS` caps the table in between (the least
//! recently active client is forgotten when a new one arrives at the cap, which forgives that
//! client's budget: the table never refuses a request for lack of room).

use std::collections::{HashMap, VecDeque};
use std::net::IpAddr;
use std::sync::Mutex;

/// Slots per window.
const BUCKETS: u64 = 96;
/// Most clients the table holds at once.
const MAX_CLIENTS: usize = 200_000;

#[derive(Default)]
struct Usage {
    /// (slot number, bytes), oldest first.
    slots: VecDeque<(u64, u64)>,
    total: u64,
}

pub struct WriteBudget {
    /// Bytes a client may write per window; 0 turns the budget off.
    limit: u64,
    window_ms: u64,
    slot_ms: u64,
    clients: Mutex<HashMap<IpAddr, Usage>>,
}

impl WriteBudget {
    /// `limit == 0` means unlimited (nothing is tracked).
    pub fn new(limit: u64, window_ms: u64) -> WriteBudget {
        let window_ms = window_ms.max(BUCKETS);
        WriteBudget {
            limit,
            window_ms,
            slot_ms: window_ms / BUCKETS,
            clients: Mutex::new(HashMap::new()),
        }
    }

    pub fn enabled(&self) -> bool {
        self.limit > 0
    }

    /// When a slot stops counting.
    fn expiry(&self, slot: u64) -> u64 {
        (slot + 1) * self.slot_ms + self.window_ms
    }

    fn expire(&self, usage: &mut Usage, now_ms: u64) {
        while let Some(&(slot, bytes)) = usage.slots.front() {
            if self.expiry(slot) > now_ms {
                break;
            }
            usage.total -= bytes;
            usage.slots.pop_front();
        }
    }

    /// Counts `bytes` against the client's budget, or refuses and says in how many seconds
    /// enough of its older writes will have left the window (the whole window when `bytes`
    /// alone is over the limit). A refused write counts nothing.
    pub fn charge(&self, ip: IpAddr, bytes: u64, now_ms: u64) -> Result<(), u64> {
        if self.limit == 0 || bytes == 0 {
            return Ok(());
        }
        let key = client_key(ip);
        let mut map = self.clients.lock().unwrap_or_else(|e| e.into_inner());
        if !map.contains_key(&key) && map.len() >= MAX_CLIENTS {
            map.retain(|_, u| {
                u.slots
                    .back()
                    .is_some_and(|&(slot, _)| self.expiry(slot) > now_ms)
            });
            if map.len() >= MAX_CLIENTS {
                let oldest = map
                    .iter()
                    .min_by_key(|(_, u)| u.slots.back().map_or(0, |s| s.0))
                    .map(|(k, _)| *k);
                if let Some(k) = oldest {
                    map.remove(&k);
                }
            }
        }
        let usage = map.entry(key).or_default();
        self.expire(usage, now_ms);
        if usage.total.saturating_add(bytes) > self.limit {
            let secs = if bytes > self.limit {
                self.window_ms.div_ceil(1000)
            } else {
                let need = usage.total + bytes - self.limit;
                let mut freed = 0;
                let mut at = now_ms + self.window_ms;
                for &(slot, b) in &usage.slots {
                    freed += b;
                    if freed >= need {
                        at = self.expiry(slot);
                        break;
                    }
                }
                at.saturating_sub(now_ms).div_ceil(1000)
            };
            if usage.slots.is_empty() {
                map.remove(&key);
            }
            return Err(secs.max(1));
        }
        let slot = now_ms / self.slot_ms;
        match usage.slots.back_mut() {
            Some((s, b)) if *s == slot => *b += bytes,
            _ => usage.slots.push_back((slot, bytes)),
        }
        usage.total += bytes;
        Ok(())
    }

    /// Forgets clients with nothing left in the window.
    pub fn forget_idle(&self, now_ms: u64) {
        let mut map = self.clients.lock().unwrap_or_else(|e| e.into_inner());
        map.retain(|_, u| {
            self.expire(u, now_ms);
            !u.slots.is_empty()
        });
    }

    /// Clients currently tracked (for tests).
    pub fn tracked(&self) -> usize {
        self.clients.lock().unwrap_or_else(|e| e.into_inner()).len()
    }
}

/// An IPv6 client is its /64; an IPv4 client is its address.
pub fn client_key(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => {
            let mut o = v6.octets();
            o[8..].fill(0);
            IpAddr::from(o)
        }
        v4 => v4,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOUR: u64 = 3_600_000;
    const DAY: u64 = 24 * HOUR;

    fn ip(last: u8) -> IpAddr {
        IpAddr::from([10, 0, 0, last])
    }

    #[test]
    fn a_client_may_write_up_to_the_limit_then_waits() {
        let b = WriteBudget::new(1000, DAY);
        let t = 5 * DAY;
        assert_eq!(b.charge(ip(1), 600, t), Ok(()));
        assert_eq!(b.charge(ip(1), 400, t + 1000), Ok(()));
        let wait = b.charge(ip(1), 1, t + 2000).unwrap_err();
        // The first write leaves the window a day (and under one slot) after it was made.
        assert!(wait > 86_300 && wait <= 86_400 + 900, "{wait}");
        // Another client has its own budget.
        assert_eq!(b.charge(ip(2), 1000, t + 2000), Ok(()));
    }

    #[test]
    fn a_refused_write_counts_nothing_and_the_window_rolls() {
        let b = WriteBudget::new(1000, DAY);
        let t = 5 * DAY;
        assert_eq!(b.charge(ip(1), 700, t), Ok(()));
        assert!(b.charge(ip(1), 700, t + HOUR).is_err());
        assert!(b.charge(ip(1), 700, t + 2 * HOUR).is_err());
        assert_eq!(b.charge(ip(1), 300, t + HOUR), Ok(()));
        // A day and half an hour later the first 700 are out, the 300 are not.
        let later = t + DAY + HOUR / 2;
        assert_eq!(b.charge(ip(1), 700, later), Ok(()));
        assert!(b.charge(ip(1), 1, later).is_err());
    }

    #[test]
    fn retry_after_names_the_slot_that_frees_enough() {
        let b = WriteBudget::new(1000, DAY);
        let t = 5 * DAY;
        assert_eq!(b.charge(ip(1), 500, t), Ok(()));
        assert_eq!(b.charge(ip(1), 500, t + 10 * HOUR), Ok(()));
        // 1000 used. Asking for 400 needs the first slot to leave: ~14 h from t + 10 h.
        let wait = b.charge(ip(1), 400, t + 10 * HOUR).unwrap_err();
        assert!(wait > 13 * 3600 && wait <= 14 * 3600 + 900, "{wait}");
        // Asking for 600 needs both.
        let wait = b.charge(ip(1), 600, t + 10 * HOUR).unwrap_err();
        assert!(wait > 24 * 3600 && wait <= 24 * 3600 + 900, "{wait}");
    }

    #[test]
    fn one_write_over_the_limit_is_never_allowed() {
        let b = WriteBudget::new(1000, DAY);
        assert_eq!(b.charge(ip(1), 1001, 5 * DAY), Err(86_400));
        assert_eq!(b.tracked(), 0);
    }

    #[test]
    fn zero_turns_it_off_and_tracks_nothing() {
        let b = WriteBudget::new(0, DAY);
        assert!(!b.enabled());
        assert_eq!(b.charge(ip(1), u64::MAX, 5 * DAY), Ok(()));
        assert_eq!(b.tracked(), 0);
    }

    #[test]
    fn an_ipv6_client_is_its_slash_64() {
        let b = WriteBudget::new(100, DAY);
        let a: IpAddr = "2001:db8:1:2::1".parse().unwrap();
        let same: IpAddr = "2001:db8:1:2:ffff::9".parse().unwrap();
        let other: IpAddr = "2001:db8:1:3::1".parse().unwrap();
        assert_eq!(b.charge(a, 100, 5 * DAY), Ok(()));
        assert!(b.charge(same, 1, 5 * DAY).is_err());
        assert_eq!(b.charge(other, 100, 5 * DAY), Ok(()));
    }

    #[test]
    fn idle_clients_are_forgotten() {
        let b = WriteBudget::new(100, DAY);
        assert_eq!(b.charge(ip(1), 10, 5 * DAY), Ok(()));
        assert_eq!(b.charge(ip(2), 10, 5 * DAY + 20 * HOUR), Ok(()));
        b.forget_idle(5 * DAY + 12 * HOUR);
        assert_eq!(b.tracked(), 2);
        b.forget_idle(6 * DAY + 2 * HOUR);
        assert_eq!(b.tracked(), 1);
        b.forget_idle(8 * DAY);
        assert_eq!(b.tracked(), 0);
    }
}
