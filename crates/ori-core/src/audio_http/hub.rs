//! Bounded fan-out. Queue pressure affects delivery, never accepted recording.
use super::{
    MAX_QUEUED_BLOCKS, MAX_SUBSCRIBERS,
    budget::{Budget, Reservation},
    capacity, stream_error,
};
use crate::error::ApiResult;
use crate::security::AuthReceipt;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    mem::size_of,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio::sync::{Notify, OwnedSemaphorePermit, Semaphore};

pub(super) type Key = (String, String);
pub(super) struct Packet {
    sequence: u64,
    bytes: Box<[u8]>,
    _capacity: Reservation,
}
impl Packet {
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
}
struct Queue {
    packets: VecDeque<Arc<Packet>>,
    dropped: u64,
}
pub(super) struct Subscriber {
    queue: Mutex<Queue>,
    active: AtomicBool,
    changed: Notify,
    authority: AuthReceipt,
    _capacity: Reservation,
    _lease: OwnedSemaphorePermit,
}
impl Subscriber {
    fn new(
        budget: &Arc<Budget>,
        lease: OwnedSemaphorePermit,
        authority: AuthReceipt,
    ) -> ApiResult<Arc<Self>> {
        let reservation = budget.reserve(MAX_QUEUED_BLOCKS * size_of::<Arc<Packet>>() + 512)?;
        let mut packets = VecDeque::new();
        packets
            .try_reserve_exact(MAX_QUEUED_BLOCKS)
            .map_err(|_| capacity())?;
        Ok(Arc::new(Self {
            queue: Mutex::new(Queue {
                packets,
                dropped: 0,
            }),
            active: AtomicBool::new(true),
            changed: Notify::new(),
            authority,
            _capacity: reservation,
            _lease: lease,
        }))
    }
    pub fn active(&self) -> bool {
        self.active.load(Ordering::Acquire) && self.authority.revalidate().is_ok()
    }
    pub fn revoke(&self) {
        self.active.store(false, Ordering::Release);
        self.queue
            .lock()
            .expect("audio subscriber coordinator")
            .packets
            .clear();
        self.changed.notify_waiters();
    }
    pub async fn receive(&self) -> Option<Arc<Packet>> {
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            {
                let mut queue = self.queue.lock().expect("audio subscriber coordinator");
                if !self.active() {
                    return None;
                }
                if let Some(packet) = queue.packets.pop_front() {
                    return Some(packet);
                }
            }
            changed.await;
        }
    }
    fn offer(&self, packet: Arc<Packet>) {
        let mut queue = self.queue.lock().expect("audio subscriber coordinator");
        if !self.active() {
            return;
        }
        if queue.packets.len() == MAX_QUEUED_BLOCKS {
            queue.packets.pop_front();
            queue.dropped = queue.dropped.saturating_add(1);
        }
        queue.packets.push_back(packet);
        drop(queue);
        self.changed.notify_one();
    }
    fn drop_new(&self) {
        let mut queue = self.queue.lock().expect("audio subscriber coordinator");
        queue.dropped = queue.dropped.saturating_add(1);
    }
}

// A body owns this guard even before its first poll. Disconnection unregisters
// immediately without an async task or an ownership cycle through Core.
pub(super) struct Subscription(pub Arc<Subscriber>);
impl Drop for Subscription {
    fn drop(&mut self) {
        self.0.revoke();
    }
}

pub(super) struct Hub {
    subscribers: BTreeMap<Key, Vec<Weak<Subscriber>>>,
    budget: Arc<Budget>,
    sequence: u64,
}
impl Hub {
    pub fn new(budget: Arc<Budget>) -> Self {
        Self {
            subscribers: BTreeMap::new(),
            budget,
            sequence: 0,
        }
    }
    fn current(&mut self, key: &Key) -> Vec<Arc<Subscriber>> {
        let Some(items) = self.subscribers.get_mut(key) else {
            return Vec::new();
        };
        items.retain(|item| {
            item.upgrade().is_some_and(|sub| {
                if sub.active() {
                    true
                } else {
                    sub.revoke();
                    false
                }
            })
        });
        items.iter().filter_map(Weak::upgrade).collect()
    }
    pub fn subscribe(
        &mut self,
        key: Key,
        leases: &Arc<Semaphore>,
        authority: AuthReceipt,
    ) -> ApiResult<Subscription> {
        if self.current(&key).len() >= MAX_SUBSCRIBERS {
            return Err(stream_error(format!(
                "Не более {MAX_SUBSCRIBERS} подписчиков на поток"
            )));
        }
        let lease = leases.clone().try_acquire_owned().map_err(|_| capacity())?;
        let subscriber = Subscriber::new(&self.budget, lease, authority)?;
        let items = self
            .subscribers
            .entry(key)
            .or_insert_with(|| Vec::with_capacity(MAX_SUBSCRIBERS));
        items.push(Arc::downgrade(&subscriber));
        Ok(Subscription(subscriber))
    }
    pub fn status(&mut self, key: &Key) -> Value {
        let subscribers = self.current(key);
        let (mut queued, mut dropped) = (0usize, 0u64);
        for sub in &subscribers {
            let queue = sub.queue.lock().expect("audio subscriber coordinator");
            queued += queue.packets.len();
            dropped = dropped.saturating_add(queue.dropped);
        }
        json!({"session_id": key.0, "stream_id": key.1, "subscribers": subscribers.len(),
            "max_subscribers": MAX_SUBSCRIBERS, "queued_blocks": queued, "dropped_blocks": dropped})
    }
    pub fn publish(&mut self, key: &Key, samples: &[i16]) -> ApiResult<()> {
        if samples.len() > super::pcm::MAX_LIVE_FRAMES {
            return Err(super::stream_unavailable(format!(
                "Блок не больше {} отсчётов",
                super::pcm::MAX_LIVE_FRAMES
            )));
        }
        let subscribers = self.current(key);
        if subscribers.is_empty() {
            return Ok(());
        }
        let reservation = loop {
            match self.budget.reserve(samples.len() * 2 + 128) {
                Ok(reservation) => break Some(reservation),
                Err(_) if self.drop_oldest() => continue,
                // Pruning a revoked subscriber can free its queued packets
                // even when no live queue remains to evict.
                Err(_) => break self.budget.reserve(samples.len() * 2 + 128).ok(),
            }
        };
        let Some(reservation) = reservation else {
            for sub in subscribers {
                sub.drop_new();
            }
            return Ok(());
        };
        let mut bytes = Vec::new();
        if bytes.try_reserve_exact(samples.len() * 2).is_err() {
            for sub in subscribers {
                sub.drop_new();
            }
            return Ok(());
        }
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        self.sequence = self.sequence.saturating_add(1);
        let packet = Arc::new(Packet {
            sequence: self.sequence,
            bytes: bytes.into_boxed_slice(),
            _capacity: reservation,
        });
        for sub in subscribers {
            sub.offer(packet.clone());
        }
        Ok(())
    }
    fn drop_oldest(&mut self) -> bool {
        let mut oldest: Option<(u64, Arc<Subscriber>)> = None;
        for items in self.subscribers.values_mut() {
            items.retain(|item| {
                item.upgrade().is_some_and(|sub| {
                    if sub.active() {
                        true
                    } else {
                        sub.revoke();
                        false
                    }
                })
            });
            for sub in items.iter().filter_map(Weak::upgrade) {
                let sequence = sub
                    .queue
                    .lock()
                    .expect("audio subscriber coordinator")
                    .packets
                    .front()
                    .map(|packet| packet.sequence);
                if let Some(sequence) = sequence
                    && oldest
                        .as_ref()
                        .is_none_or(|(previous, _)| sequence < *previous)
                {
                    oldest = Some((sequence, sub));
                }
            }
        }
        let Some((_, sub)) = oldest else {
            return false;
        };
        let mut queue = sub.queue.lock().expect("audio subscriber coordinator");
        if queue.packets.pop_front().is_some() {
            queue.dropped = queue.dropped.saturating_add(1);
        }
        // A concurrent body read can already have freed the selected packet.
        true
    }
    pub fn close(&mut self) {
        for items in self.subscribers.values() {
            for sub in items.iter().filter_map(Weak::upgrade) {
                sub.revoke();
            }
        }
        self.subscribers.clear();
    }
}
