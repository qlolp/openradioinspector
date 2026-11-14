//! Fixed history and coalesced recordings; published storage is immutable.
use super::{
    HISTORY_FRAMES, MAX_RECORDING_FRAMES, RATE,
    budget::{Budget, Reservation},
    capacity, phonogram,
};
use crate::error::ApiResult;
use serde_json::{Value, json};
use std::{mem::size_of, sync::Arc};

const CHUNK_BYTES: usize = 64 * 1024;

struct Ring {
    samples: Vec<i16>,
    next: usize,
    len: usize,
    _capacity: Reservation,
}
impl Ring {
    fn new(frames: usize, budget: &Arc<Budget>) -> ApiResult<Self> {
        let reservation = budget.reserve(frames * size_of::<i16>() + 128)?;
        let mut samples = Vec::new();
        samples.try_reserve_exact(frames).map_err(|_| capacity())?;
        samples.resize(frames, 0);
        Ok(Self {
            samples,
            next: 0,
            len: 0,
            _capacity: reservation,
        })
    }
    fn push(&mut self, block: &[i16]) {
        if self.samples.is_empty() {
            return;
        }
        for sample in block {
            self.samples[self.next] = *sample;
            self.next = (self.next + 1) % self.samples.len();
        }
        self.len = self.samples.len().min(self.len.saturating_add(block.len()));
    }
    fn iter(&self) -> impl Iterator<Item = i16> + '_ {
        let start = if self.len == self.samples.len() {
            self.next
        } else {
            0
        };
        (0..self.len).map(move |i| self.samples[(start + i) % self.samples.len()])
    }
}

pub(super) struct Chunk {
    bytes: Box<[u8]>,
    used: usize,
    _capacity: Reservation,
}
impl Chunk {
    fn new(capacity_bytes: usize, budget: &Arc<Budget>) -> ApiResult<Arc<Self>> {
        // Include a conservative charge for the object and Arc allocation.
        let reservation = budget.reserve(capacity_bytes + 128)?;
        let mut bytes = Vec::new();
        bytes
            .try_reserve_exact(capacity_bytes)
            .map_err(|_| capacity())?;
        bytes.resize(capacity_bytes, 0);
        Ok(Arc::new(Self {
            bytes: bytes.into_boxed_slice(),
            used: 0,
            _capacity: reservation,
        }))
    }
    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..self.used]
    }
}

struct Recording {
    chunks: Vec<Arc<Chunk>>,
    frames: usize,
    pre_frames: usize,
    started_at: String,
    chunk_bytes: usize,
    _metadata: Reservation,
}
impl Recording {
    fn new(
        max_frames: usize,
        pre_frames: usize,
        started_at: &str,
        chunk_bytes: usize,
        budget: &Arc<Budget>,
    ) -> ApiResult<Self> {
        let max_chunks = (max_frames * 2).div_ceil(chunk_bytes);
        let metadata = budget.reserve(max_chunks * size_of::<Arc<Chunk>>() + 512)?;
        let mut chunks = Vec::new();
        chunks
            .try_reserve_exact(max_chunks)
            .map_err(|_| capacity())?;
        Ok(Self {
            chunks,
            frames: 0,
            pre_frames,
            started_at: started_at.into(),
            chunk_bytes,
            _metadata: metadata,
        })
    }
    fn prepare(&self, frames: usize, budget: &Arc<Budget>) -> ApiResult<Vec<Arc<Chunk>>> {
        let available = self
            .chunks
            .last()
            .map_or(0, |chunk| chunk.bytes.len() - chunk.used);
        let count = (frames * 2)
            .saturating_sub(available)
            .div_ceil(self.chunk_bytes);
        let mut chunks = Vec::new();
        chunks.try_reserve_exact(count).map_err(|_| capacity())?;
        for _ in 0..count {
            chunks.push(Chunk::new(self.chunk_bytes, budget)?);
        }
        Ok(chunks)
    }
    fn append(&mut self, samples: impl Iterator<Item = i16>, prepared: Vec<Arc<Chunk>>) {
        let mut prepared = prepared.into_iter();
        for sample in samples {
            if self
                .chunks
                .last()
                .is_none_or(|chunk| chunk.used == chunk.bytes.len())
            {
                self.chunks.push(
                    prepared
                        .next()
                        .expect("all recording capacity reserved before mutation"),
                );
            }
            let tail = Arc::get_mut(self.chunks.last_mut().expect("reserved recording chunk"))
                .expect("unfinished recording is never published");
            tail.bytes[tail.used..tail.used + 2].copy_from_slice(&sample.to_le_bytes());
            tail.used += 2;
            self.frames += 1;
        }
        debug_assert!(prepared.next().is_none());
    }
}

pub(super) struct Completed {
    recording: Recording,
    ended_at: String,
}
impl Completed {
    pub fn summary(&self) -> Value {
        let record = &self.recording;
        json!({"sample_rate_hz": RATE, "pre_history_samples": record.pre_frames,
            "recorded_samples": record.frames - record.pre_frames,
            "pre_history_seconds": record.pre_frames as f64 / f64::from(RATE),
            "recorded_seconds": (record.frames - record.pre_frames) as f64 / f64::from(RATE),
            "duration_seconds": record.frames as f64 / f64::from(RATE),
            "started_at": record.started_at, "ended_at": self.ended_at})
    }
    pub fn length(&self) -> usize {
        44 + self.recording.frames * 2
    }
    pub fn chunks(&self) -> &[Arc<Chunk>] {
        &self.recording.chunks
    }
    pub fn header(&self) -> [u8; 44] {
        let data_bytes = (self.recording.frames * 2) as u32;
        let mut header = [0; 44];
        header[0..4].copy_from_slice(b"RIFF");
        header[4..8].copy_from_slice(&(36 + data_bytes).to_le_bytes());
        header[8..16].copy_from_slice(b"WAVEfmt ");
        header[16..20].copy_from_slice(&16u32.to_le_bytes());
        header[20..22].copy_from_slice(&1u16.to_le_bytes());
        header[22..24].copy_from_slice(&1u16.to_le_bytes());
        header[24..28].copy_from_slice(&RATE.to_le_bytes());
        header[28..32].copy_from_slice(&(RATE * 2).to_le_bytes());
        header[32..34].copy_from_slice(&2u16.to_le_bytes());
        header[34..36].copy_from_slice(&16u16.to_le_bytes());
        header[36..40].copy_from_slice(b"data");
        header[40..44].copy_from_slice(&data_bytes.to_le_bytes());
        header
    }
    #[cfg(test)]
    pub fn pre_history_frames(&self) -> usize {
        self.recording.pre_frames
    }
    #[cfg(test)]
    pub fn pcm_bytes(&self) -> Vec<u8> {
        self.chunks()
            .iter()
            .flat_map(|chunk| chunk.bytes().iter().copied())
            .collect()
    }
}

pub(super) struct Recorder {
    ring: Ring,
    recording: Option<Recording>,
    last: Option<Arc<Completed>>,
    max_frames: usize,
    chunk_bytes: usize,
    budget: Arc<Budget>,
}
impl Recorder {
    pub fn new(budget: Arc<Budget>) -> ApiResult<Self> {
        Self::with_limits(HISTORY_FRAMES, MAX_RECORDING_FRAMES, budget)
    }
    fn with_limits(history: usize, max_frames: usize, budget: Arc<Budget>) -> ApiResult<Self> {
        let ring = Ring::new(history, &budget)?;
        Ok(Self {
            ring,
            recording: None,
            last: None,
            max_frames,
            chunk_bytes: CHUNK_BYTES.min((max_frames * 2).max(2)),
            budget,
        })
    }
    pub fn push(&mut self, samples: &[i16], now: &str) -> ApiResult<()> {
        if samples.is_empty() {
            return Ok(());
        }
        let (accepted, prepared) = if let Some(recording) = &self.recording {
            let accepted = samples
                .len()
                .min(self.max_frames.saturating_sub(recording.frames));
            (accepted, recording.prepare(accepted, &self.budget)?)
        } else {
            (0, Vec::new())
        };
        // No fallible allocation after the first state change. The complete
        // live block enters history even when recording ends partway through.
        self.ring.push(samples);
        if let Some(recording) = &mut self.recording {
            recording.append(samples[..accepted].iter().copied(), prepared);
            if recording.frames == self.max_frames {
                self.finish(now);
            }
        }
        Ok(())
    }
    pub fn start(&mut self, now: &str) -> ApiResult<()> {
        if self.recording.is_some() {
            return Err(phonogram("Запись уже идёт"));
        }
        let mut recording = Recording::new(
            self.max_frames,
            self.ring.len,
            now,
            self.chunk_bytes,
            &self.budget,
        )?;
        let prepared = recording.prepare(self.ring.len, &self.budget)?;
        recording.append(self.ring.iter(), prepared);
        self.recording = Some(recording);
        self.last = None;
        Ok(())
    }
    pub fn stop(&mut self, now: &str) -> ApiResult<()> {
        if self.recording.is_none() {
            return Err(phonogram("Запись не начата"));
        }
        self.finish(now);
        Ok(())
    }
    fn finish(&mut self, now: &str) {
        let recording = self.recording.take().expect("active recording");
        self.last = Some(Arc::new(Completed {
            recording,
            ended_at: now.into(),
        }));
    }
    pub fn completed(&self) -> ApiResult<Arc<Completed>> {
        self.last
            .clone()
            .ok_or_else(|| phonogram("Фонограмма ещё не записана"))
    }
    pub fn status(&self) -> Value {
        json!({"sample_rate_hz": RATE, "recording": self.recording.is_some(),
            "pre_history_seconds": 5.0, "buffered_seconds": self.ring.len as f64 / f64::from(RATE),
            "recorded_seconds": self.recording.as_ref().map_or(0.0, |r| (r.frames - r.pre_frames) as f64 / f64::from(RATE)),
            "started_at": self.recording.as_ref().map(|r| &r.started_at),
            "last": self.last.as_ref().map(|last| last.summary())})
    }
    pub fn empty_status() -> Value {
        json!({"sample_rate_hz": RATE, "recording": false, "pre_history_seconds": 5.0,
            "buffered_seconds": 0.0, "recorded_seconds": 0.0, "started_at": null, "last": null})
    }
    #[cfg(test)]
    pub fn new_for_test(history: usize, max_frames: usize, bytes: usize) -> ApiResult<Self> {
        Self::with_limits(history, max_frames, Budget::new(bytes))
    }
    #[cfg(test)]
    pub fn with_test_budget(
        history: usize,
        max_frames: usize,
        budget: Arc<Budget>,
    ) -> ApiResult<Self> {
        Self::with_limits(history, max_frames, budget)
    }
}
